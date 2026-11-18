"""Own the ForSLM service lifecycle and coordinate model and Pipe requests."""
import argparse
import asyncio
from contextlib import closing, suppress
from dataclasses import dataclass
import hmac
import importlib.util
import logging
import msvcrt
import os
from pathlib import Path
import secrets
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
from urllib.parse import urlsplit
from uuid import uuid4

from aiohttp import ClientError, ClientResponseError, ClientSession, ClientTimeout, web, WSMsgType
import httpx

from managed_browser import Browser, BrowserSession, path_key
from model_gateway import Route, forward, model_command
from runtime_config import (
    DEFAULT_URLS, LOCAL, RESOLVED, ROOT, RUNTIME, local_file, read_json, read_local, resolve, write_json,
)
from windows_job import Job

log = logging.getLogger("ForSLM")
WEBUI = "http://127.0.0.1:3000"
CONTROL = RUNTIME / "manager.json"
LOCK = RUNTIME / "manager.lock"
WEBUI_PYTHON = RUNTIME / "webui-env/Scripts/python.exe"
SEARX_PYTHON = RUNTIME / "searxng-env/Scripts/python.exe"
SCRIPTS = ROOT / "integration"
LOGS = ROOT / "logs"
STARTUP_SECONDS = 300
WEBUI_MODEL_KEY = "local-llama"
QWEN_PROVIDER_URL = "forslm_qwen_url"

registration = importlib.util.spec_from_file_location("registration", SCRIPTS / "register-pipe.py")
pipe = importlib.util.module_from_spec(registration)
registration.loader.exec_module(pipe)


def bind_socket(port=0):
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        sock.bind(("127.0.0.1", port))
        return sock
    except OSError:
        sock.close()
        raise


def occupied(port):
    try:
        with bind_socket(port):
            return False
    except OSError as error:
        if error.winerror not in (10013, 10048):
            raise
        return True


def acquire(file):
    file.seek(0)
    try:
        msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        return False
    return True


def manager_exists():
    with LOCK.open("a+b") as file:
        return not acquire(file)


async def start_http(app, port=0):
    runner = web.AppRunner(app, handler_cancellation=True, shutdown_timeout=1)
    await runner.setup()
    try:
        sock = bind_socket(port)
        try:
            await web.SockSite(runner, sock).start()
            return runner, sock.getsockname()[1]
        except BaseException:
            sock.close()
            raise
    except BaseException:
        await runner.cleanup()
        raise


@dataclass
class PipeTask:
    socket: web.WebSocketResponse
    deadline: float
    state: str = "running"


def verify_database():
    history = ROOT / "data/open-webui/webui.db"
    if history.exists():
        with closing(sqlite3.connect(history.as_uri() + "?mode=ro", uri=True, timeout=5)) as database:
            database.execute("SELECT name FROM sqlite_schema").fetchall()


def plan_webui_models(config, expected):
    urls = config.get("OPENAI_API_BASE_URLS") or []
    saved = config.get("OPENAI_API_CONFIGS") or {}
    keys = config.get("OPENAI_API_KEYS") or []
    entries = {str(i): dict(saved.get(str(i), saved.get(url, {}))) for i, url in enumerate(urls)}
    next_urls = list(urls)
    for index, url in enumerate(urls):
        entry = entries[str(index)]
        managed_url = entry.get(QWEN_PROVIDER_URL, DEFAULT_URLS["qwen_url"])
        if url != expected and url == managed_url and index < len(keys) and keys[index] == WEBUI_MODEL_KEY:
            next_urls[index] = expected
            entry[QWEN_PROVIDER_URL] = expected
    return next_urls, entries


class Manager:
    def __init__(self, args, job):
        self.args = args
        self.job = job
        self.stop_requested = asyncio.Event()
        self.unload_finished = asyncio.Event()
        self.stop_id = uuid4().hex
        self.token = secrets.token_hex(32)
        self.phase = "starting"
        self.component = "manager"
        self.error = None
        self.tasks = {}
        self.model_requests = set()
        self.routes = {}
        self.runners = []
        self.processes = {}
        self.unloading = False
        self.settings = {}
        self.browser = BrowserSession(args.browser) if args.browser else None
        self.stop_reason = None

    @property
    def stopping(self):
        return self.stop_requested.is_set()

    @property
    def headers(self):
        return {"X-ForSLM-Token": self.token}

    @property
    def control(self):
        return {"url": f"http://127.0.0.1:{self.control_port}", "token": self.token}

    def state(self):
        return {"phase": self.phase, "component": self.component, "error": self.error,
                "launch": self.args.launch, "instance": self.stop_id,
                "tasks": len(self.tasks), "model_requests": len(self.model_requests),
                "unloading": self.unloading, "stop_reason": self.stop_reason,
                "browser": self.browser.state() if self.browser else None}

    def authenticate(self, request):
        if not hmac.compare_digest(request.headers.get("X-ForSLM-Token", "").encode(),
                                   self.token.encode()):
            raise web.HTTPForbidden()

    async def status(self, request):
        self.authenticate(request)
        return web.json_response(self.state())

    def request_stop(self, reason):
        if not self.stopping:
            self.stop_reason = reason
            log.info("Shutdown requested: %s", reason)
            self.stop_requested.set()

    async def stop(self, request):
        self.authenticate(request)
        self.request_stop("Stop.cmd")
        return web.json_response({"stopping": True})

    async def start_instance(self):
        if self.browser:
            self.component = "Managed browser preflight"
            self.browser.browser.preflight()
        await self.start_services()
        if self.browser:
            self.component = "Managed browser startup"
            await self.browser.start(WEBUI, LOGS)

    async def watch_browser(self, session):
        closed_session = await session.observe()
        if self.browser is session and session.id == closed_session:
            self.request_stop("managed_browser_closed")

    async def expire_task(self, task):
        await asyncio.sleep(max(0, task.deadline - time.monotonic()))
        task.state = "canceling"
        with suppress(ConnectionError, RuntimeError):
            await task.socket.send_str("timeout")

    async def task(self, request):
        self.authenticate(request)
        if self.stopping or self.phase != "ready":
            raise web.HTTPServiceUnavailable(text="ForSLM is not accepting tasks")
        task_id = request.match_info["id"]
        if task_id in self.tasks:
            raise web.HTTPConflict()
        websocket = web.WebSocketResponse(heartbeat=15)
        task = PipeTask(websocket, time.monotonic() + self.settings["task_timeout_seconds"])
        self.tasks[task_id] = task
        log.debug("Pipe %s: %s, deadline %.3f", task_id, task.state, task.deadline)
        deadline = asyncio.create_task(self.expire_task(task))
        try:
            await websocket.prepare(request)
            async for message in websocket:
                if message.type == WSMsgType.TEXT and message.data == "finished":
                    break
        finally:
            deadline.cancel()
            await asyncio.gather(deadline, return_exceptions=True)
            self.tasks.pop(task_id, None)
            log.debug("Pipe %s: released", task_id)
        return websocket

    def spawn(self, key, args, cwd=ROOT, env=None, flags=subprocess.CREATE_NO_WINDOW):
        with (LOGS / f"{key}.out.log").open("ab") as stdout, (LOGS / f"{key}.err.log").open("ab") as stderr:
            process = self.job.spawn(args, cwd=cwd, env=env, flags=flags,
                                     stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr)
        self.processes[key] = process
        return process

    async def json(self, method, url, **kwargs):
        async with self.client.request(method, url, timeout=ClientTimeout(total=5),
                                       allow_redirects=False, **kwargs) as response:
            response.raise_for_status()
            return await response.json()

    async def ready(self, key, url):
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            process = self.processes.get(key)
            if process and process.poll() is not None:
                raise RuntimeError(f"process exited with code {process.returncode}. See logs/{key}.err.log")
            try:
                async with self.client.get(url, timeout=ClientTimeout(total=2)) as response:
                    if response.status == 200:
                        return
            except (ClientError, TimeoutError):
                pass
            await asyncio.sleep(0.5)
        raise TimeoutError(f"{url} did not become ready. See logs/{key}.err.log")

    async def verify_models(self, key, url, external=False):
        name = self.settings[f"{key}_name"]
        api_key = os.environ.get("RERANK_API_KEY") if key == "bge" and external else None
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        info = await self.json("GET", url + "/models", headers=headers)
        entries = info.get("data")
        if not isinstance(entries, list) or not all(isinstance(entry.get("id"), str) for entry in entries):
            raise ValueError(f"{url}/models did not return a compatible model list")
        names = {entry["id"] for entry in entries}
        if name not in names:
            legacy_key = "qwen_model" if key == "qwen" else "reranker_model"
            legacy_name = Path(self.settings.get(legacy_key) or "").name
            match = next((entry["id"] for entry in entries
                          if Path(entry["id"]).name == legacy_name and legacy_name), None)
            if not external or not match:
                raise ValueError(f"{url} expected {name}; model list contains {sorted(names)}")
            self.settings[f"{key}_name"] = match
            name = match
        if key == "bge" and external:
            info = await self.json("POST", url + "/rerank", headers=headers,
                                   json={"model": name, "query": "test", "documents": ["test"]})
            if not any(entry.get("index") == 0 for entry in info.get("results", [])):
                raise ValueError(f"{url}/rerank did not return a score")

    async def start_route(self, key, path, lazy):
        public = urlsplit(DEFAULT_URLS[key + "_url"]).port
        with bind_socket() as sock:
            port = sock.getsockname()[1]
        args = model_command(self.settings, key, path, port, lazy)
        env = {key: value for key, value in os.environ.items() if not key.startswith("LLAMA_ARG_")}
        env["LLAMA_CACHE"] = str(RUNTIME / "model-cache")
        Path(env["LLAMA_CACHE"]).mkdir(exist_ok=True)
        error_log = LOGS / f"{key}.err.log"
        offset = error_log.stat().st_size if error_log.exists() else 0
        process = self.spawn(key, args, Path(args[0]).parent, env)
        route = Route(self.settings[f"{key}_name"], f"http://127.0.0.1:{port}", lazy)
        missing = None
        try:
            await self.ready(key, route.upstream + "/health")
        except RuntimeError:
            with error_log.open("rb") as stream:
                stream.seek(offset)
                diagnostics = stream.read().decode(errors="replace").lower()
            if not lazy or "subprocess is not enabled" not in diagnostics:
                raise
            missing = "router subprocess support"
        if lazy and not missing:
            missing = await self.check_router(route)
        if missing:
            if process.poll() is None:
                process.terminate()
                await asyncio.to_thread(process.wait, 10)
            log.warning("%s: this build lacks %s, using single-model mode", key, missing)
            await self.start_route(key, path, False)
            return
        await self.verify_models(key, route.upstream + "/v1")
        self.routes[key] = route
        app = web.Application(client_max_size=64 * 1024 ** 2)

        async def proxy(request):
            return await forward(self, route, request)

        app.router.add_route("*", "/{tail:.*}", proxy)
        runner, _ = await start_http(app, public)
        self.runners.append(runner)
        log.info("%s ready on %s (%s)", key, public, "lazy router" if lazy else "single model")

    async def check_router(self, route):
        try:
            if await route.state(self.client) != "unloaded":
                raise ValueError("the model router did not start unloaded")
        except KeyError:
            return "router model status"
        except ClientResponseError as error:
            if error.status != 404:
                raise
            return "router model status"
        async with self.client.post(route.upstream + "/models/unload", json={"model": route.name},
                                    timeout=ClientTimeout(total=4)) as response:
            if response.status == 404:
                return "the unload API"
            if response.status not in (200, 400):
                response.raise_for_status()
                raise ValueError("unexpected model unload response")
        return None

    async def model_capabilities(self, executable):
        def show_help():
            with Job() as probe:
                child = probe.spawn([executable, "--help"], cwd=Path(executable).parent,
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    stdin=subprocess.DEVNULL)
                output, _ = child.communicate(timeout=20)
                if child.returncode:
                    raise ValueError("llama-server --help failed. Check its runtime libraries.")
                return output.decode(errors="replace")
        return "--models-preset" in await asyncio.to_thread(show_help)

    async def searx(self, external):
        url = self.settings["searxng_url"]
        if not external:
            settings_path = ROOT / "config/searxng.yml"
            if not settings_path.exists():
                shutil.copyfile(ROOT / "config/searxng.example.yml", settings_path)
            settings = settings_path.read_text(encoding="utf-8-sig")
            if "REPLACE_ON_FIRST_START" in settings:
                settings_path.write_text(settings.replace("REPLACE_ON_FIRST_START", secrets.token_hex(32)),
                                         encoding="utf-8")
            searx_dir = ROOT / "upstream/searxng"
            self.spawn("searxng", [str(SEARX_PYTHON), str(SCRIPTS / "serve-searxng.py")], searx_dir,
                       {**os.environ, "PYTHONPATH": str(searx_dir), "SEARXNG_SETTINGS_PATH": str(settings_path)})
            await self.ready("searxng", url + "/")
        config = await self.json("GET", url + "/config")
        if not isinstance(config.get("engines"), list) or not isinstance(config.get("version"), str):
            raise ValueError(f"{url} does not identify itself as SearXNG")
        async with self.client.get(url + "/search", params={"format": "json"},
                                   timeout=ClientTimeout(total=5), allow_redirects=False) as response:
            if response.status != 400 or not (await response.json()).get("error"):
                raise ValueError(f"{url}/search did not accept the JSON format")
        log.info("SearXNG ready at %s (%s)", url, "external" if external else "project")

    async def webui(self):
        own = not occupied(3000)
        if own:
            env = {**os.environ, "USE_SLIM_DOCKER": "true", "DATA_DIR": str(ROOT / "data/open-webui"),
                   "WEBUI_AUTH": "false", "WEBUI_URL": WEBUI, "UVICORN_WORKERS": "1",
                   "NO_PROXY": ",".join(filter(None, (os.getenv("NO_PROXY"), "localhost,127.0.0.1,::1"))),
                   "ENABLE_OLLAMA_API": "false", "OPENAI_API_BASE_URL": DEFAULT_URLS["qwen_url"],
                   "OPENAI_API_KEY": WEBUI_MODEL_KEY, "DEFAULT_MODELS": "local_ai_search",
                   "ENABLE_WEB_SEARCH": "false", "ENABLE_EVALUATION_ARENA_MODELS": "false",
                   "OFFLINE_MODE": "true", "RAG_EMBEDDING_MODEL_AUTO_UPDATE": "false",
                   "WHISPER_MODEL_AUTO_UPDATE": "false", "ENABLE_VERSION_UPDATE_CHECK": "false",
                   "ANONYMIZED_TELEMETRY": "false", "DO_NOT_TRACK": "true"}
            self.spawn("open-webui", [str(WEBUI_PYTHON), str(SCRIPTS / "serve-webui.py")], env=env)
            await self.ready("open-webui", WEBUI + "/health")
        # A compatible instance reads the same project's snapshot and hosts the presence endpoint.
        presence = await self.json("GET", WEBUI + "/_forslm/presence", headers=self.headers)
        if type(presence.get("generation")) is not int:
            raise ValueError("the existing Open WebUI lacks a compatible ForSLM presence endpoint")
        auth = await pipe.sign_in(self.client, WEBUI)
        existing, valves = await pipe.inspect_pipe(self.client, WEBUI, auth)
        return auth, existing, valves

    async def check_webui_models(self, auth):
        config = await self.json("GET", WEBUI + "/openai/config", headers=auth)
        expected = self.settings["qwen_url"]
        urls, entries = plan_webui_models(config, expected)
        if urls != (config.get("OPENAI_API_BASE_URLS") or []):
            config["OPENAI_API_BASE_URLS"] = urls
            config["OPENAI_API_CONFIGS"] = entries
            await self.json("POST", WEBUI + "/openai/config/update", headers=auth, json=config)
        active = {i: url for i, url in enumerate(urls) if entries[str(i)].get("enable", True)}
        if not config.get("ENABLE_OPENAI_API"):
            log.info("Open WebUI native model providers are disabled. Pipe Qwen endpoint: %s", expected)
            return
        log.info("Open WebUI effective model endpoints (Open WebUI /openai/config): %s", list(active.values()))
        index = next((i for i, url in active.items() if url == expected), None)
        if index is not None:
            models = await self.json("GET", WEBUI + f"/openai/models/{index}", headers=auth)
            if self.settings["qwen_name"] not in {model["id"] for model in models.get("data", [])}:
                raise ValueError(f"Open WebUI cannot discover {self.settings['qwen_name']} at {expected}")
        elif active:
            models = await self.json("GET", WEBUI + "/openai/models", headers=auth)
            if not models.get("data"):
                raise ValueError(f"Open WebUI cannot discover models at {list(active.values())}")

    async def start_services(self):
        self.component = "config/local.json"
        data = read_local()
        self.settings = resolve(data, deferred_node=True)
        self.component = "installation"
        for file in (WEBUI_PYTHON, SEARX_PYTHON, ROOT / "upstream/agent-search/dist/research.js"):
            local_file(str(file), "installation")
        self.component = "Open WebUI chat database"
        verify_database()
        self.component = "Open WebUI (port 3000)"
        write_json(RESOLVED, {"control": self.control})
        auth, existing, valves = await self.webui()
        self.component = "legacy Pipe Valves"
        migrated = pipe.migrate_valves(data, valves, log)
        self.settings = resolve(migrated)
        write_json(RESOLVED, {**self.settings, "control": self.control})
        external = {}
        model_paths = {}
        for key in ("qwen", "bge", "searxng"):
            url = self.settings[key + "_url"]
            public = urlsplit(DEFAULT_URLS[key + "_url"]).port
            self.component = f"{key} ({url}, default port {public})"
            external[key] = url != DEFAULT_URLS[key + "_url"] or occupied(public)
            if external[key]:
                if key == "searxng":
                    await self.searx(True)
                else:
                    await self.verify_models(key, url, external=True)
                    log.info("%s at %s: external model verified", key, url)
            elif key != "searxng":
                path_key = "qwen_model" if key == "qwen" else "reranker_model"
                model_paths[key] = local_file(self.settings.get(path_key), path_key)
                if Path(model_paths[key]).suffix.lower() != ".gguf":
                    raise ValueError(f"{path_key}: expected a GGUF model")
                with Path(model_paths[key]).open("rb") as file:
                    if file.read(4) != b"GGUF":
                        raise ValueError(f"{path_key}: invalid GGUF header")
        if model_paths:
            self.component = "llama.cpp"
            self.settings["llama_server"] = local_file(self.settings.get("llama_server"), "llama_server")
            lazy = await self.model_capabilities(self.settings["llama_server"])
            if not lazy:
                log.warning("llama.cpp lacks --models-preset. Page-close unload is unavailable for local models.")
            for key, path in model_paths.items():
                self.component = f"{key} (port {urlsplit(DEFAULT_URLS[key + '_url']).port})"
                await self.start_route(key, path, lazy)
        if not external["searxng"]:
            self.component = "SearXNG (port 8888)"
            await self.searx(False)
        self.component = "Open WebUI model discovery (port 3000)"
        await self.check_webui_models(auth)
        self.component = "Pipe registration (port 3000)"
        write_json(RESOLVED, {**self.settings, "control": self.control, "managed_models": list(self.routes)})
        await pipe.register(self.client, WEBUI, auth, existing, self.settings["qwen_name"])
        if data != migrated:
            if read_local() != data:
                raise RuntimeError("config/local.json changed during startup. Start again to resolve its new settings.")
            write_json(LOCAL, migrated)

    async def watch_pages(self):
        lazy_routes = [route for route in self.routes.values() if route.lazy]
        if not lazy_routes:
            await self.stop_requested.wait()
            return
        disconnected = False
        checked = False
        last_generation = None
        idle_wait = False
        while True:
            await asyncio.sleep(0.5)
            try:
                presence = await self.json("GET", WEBUI + "/_forslm/presence", headers=self.headers)
                live = presence["count"]
                empty = presence["empty_seconds"]
                generation = presence["generation"]
                if disconnected:
                    log.info("Open WebUI page presence recovered")
                    disconnected = False
            except (ClientError, TimeoutError, KeyError, ValueError) as error:
                if not disconnected:
                    log.warning("Auto-unload delayed: page presence unavailable: %s", error)
                disconnected = True
                continue
            if live or empty is None or generation != last_generation:
                checked = False
                idle_wait = False
            last_generation = generation
            if not live and not checked and empty is not None and empty >= self.settings["unload_grace_seconds"]:
                if self.stopping or self.tasks or self.model_requests:
                    continue
                try:
                    idle = await asyncio.gather(*(route.idle(self.client) for route in lazy_routes))
                    if not all(idle):
                        continue
                    current = await self.json("GET", WEBUI + "/_forslm/presence", headers=self.headers)
                except (ClientError, TimeoutError, ValueError, KeyError, TypeError) as error:
                    if not idle_wait:
                        log.warning("Auto-unload delayed: could not confirm page and model state: %s", error)
                    idle_wait = True
                    continue
                if current["count"] or current["generation"] != generation:
                    continue
                if self.stopping or self.tasks or self.model_requests:
                    continue
                self.unloading = True
                self.unload_finished.clear()
                log.info("Last page disconnected. Unloading idle models.")
                try:
                    await asyncio.gather(*(route.unload(self.client) for route in lazy_routes))
                    checked = True
                finally:
                    self.unloading = False
                    self.unload_finished.set()

    async def watch_services(self):
        while True:
            for key, process in self.processes.items():
                if process.poll() is not None:
                    raise RuntimeError(f"{key} exited with code {process.returncode}. See logs/{key}.err.log")
            await asyncio.sleep(1)

    async def drain(self):
        self.stop_requested.set()
        grace = self.settings.get("shutdown_grace_seconds", 15)
        deadline = time.monotonic() + grace
        log.info("Stopping. Waiting for %s Pipe and %s model requests.", len(self.tasks), len(self.model_requests))
        while (self.tasks or self.model_requests) and time.monotonic() < deadline:
            await asyncio.sleep(0.1)
        if self.tasks or self.model_requests:
            for task in list(self.tasks.values()):
                task.state = "canceling"
                with suppress(ConnectionError, RuntimeError):
                    await task.socket.send_str("cancel")
            for process in list(self.model_requests):
                process.cancel()
            deadline = time.monotonic() + 8
            while (self.tasks or self.model_requests) and time.monotonic() < deadline:
                await asyncio.sleep(0.1)

    async def close_webui(self):
        process = self.processes.get("open-webui")
        if process is None or process.poll() is not None:
            return
        try:
            async with self.client.post(
                WEBUI + "/_forslm/shutdown", headers=self.headers, timeout=ClientTimeout(total=1)
            ) as response:
                response.raise_for_status()
            async with asyncio.timeout(5):
                while process.poll() is None:
                    await asyncio.sleep(0.1)
            if process.returncode:
                log.warning("Open WebUI exited with code %s", process.returncode)
            else:
                log.info("Open WebUI stopped gracefully")
        except (ClientError, TimeoutError) as error:
            log.warning("Open WebUI graceful shutdown incomplete: %s", error or "deadline elapsed")

    async def run(self):
        app = web.Application()
        app.router.add_get("/status", self.status)
        app.router.add_post("/stop", self.stop)
        app.router.add_get("/tasks/{id}", self.task)
        runner, self.control_port = await start_http(app)
        write_json(CONTROL, {**self.control, **self.state()})
        async with ClientSession(timeout=ClientTimeout(total=None, sock_connect=5, sock_read=420),
                                 auto_decompress=False, skip_auto_headers={"User-Agent", "Accept-Encoding"}) as self.client:
            stopping = asyncio.create_task(self.stop_requested.wait())
            startup = asyncio.create_task(self.start_instance())
            runtime = []
            try:
                ready, _ = await asyncio.wait((startup, stopping), timeout=STARTUP_SECONDS - 5,
                                              return_when=asyncio.FIRST_COMPLETED)
                if not ready:
                    raise TimeoutError("startup deadline elapsed")
                if not self.stopping:
                    await startup
                    self.phase = "ready"
                    self.component = "services"
                    log.info("Open WebUI: %s", WEBUI)
                    runtime = [asyncio.create_task(self.watch_pages()), asyncio.create_task(self.watch_services())]
                    if self.browser:
                        runtime.append(asyncio.create_task(self.watch_browser(self.browser)))
                    await asyncio.wait([stopping, *runtime], return_when=asyncio.FIRST_COMPLETED)
                    for task in runtime:
                        if task.done():
                            task.result()
                self.phase = "stopping"
            except Exception as error:
                self.phase = "failed"
                self.error = f"{self.component}: {error}"
                if self.browser and self.component.startswith("Managed browser"):
                    self.browser.phase, self.browser.error = "failed", str(error)
                self.request_stop("startup_failed" if not runtime else "service_failed")
                log.exception("%s", self.error)
            finally:
                stopping.cancel()
                startup.cancel()
                for task in runtime:
                    task.cancel()
                await asyncio.gather(stopping, startup, *runtime, return_exceptions=True)
                if self.browser:
                    outcomes = await asyncio.gather(self.browser.close(), self.drain(), return_exceptions=True)
                    for component, result in zip(("Browser cleanup", "Task drain"), outcomes):
                        if isinstance(result, Exception):
                            log.error("%s: %s", component, result)
                else:
                    await self.drain()
                await self.close_webui()
                for server in reversed(self.runners):
                    await server.cleanup()
                await asyncio.to_thread(self.job.terminate)
                for process in self.processes.values():
                    process.wait(timeout=2)
                write_json(CONTROL, {**self.control, **self.state(),
                                     "phase": "failed" if self.error else "stopped"})
                await runner.cleanup()


def control_request(client, method="GET", route="/status", **kwargs):
    try:
        current = read_json(CONTROL)
        response = client.request(method, current["url"] + route, headers={"X-ForSLM-Token": current["token"]}, **kwargs)
        response.raise_for_status()
        return response.json()
    except (OSError, ValueError, KeyError, httpx.HTTPError):
        return None


def check_browser_mode(browser, status):
    if browser is None:
        return
    bound = status.get("browser")
    if not bound or path_key(bound["path"]) != path_key(browser.exe):
        raise RuntimeError("A different ForSLM instance is running. Run Stop.cmd before changing browser mode.")


def launch(args, client):
    status = control_request(client)
    child = None
    launch_id = uuid4().hex
    if not status and not manager_exists():
        command = [sys.executable, str(Path(__file__).resolve()), "run", "--launch", launch_id]
        if args.browser:
            args.browser.preflight()
            command.extend(("--browser", str(args.browser.exe)))
        with (LOGS / "manager.log").open("ab") as log_file:
            child = subprocess.Popen(command, cwd=ROOT, stdin=subprocess.DEVNULL, stdout=log_file, stderr=log_file,
                                     creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP)
    deadline = time.monotonic() + STARTUP_SECONDS
    last_component = None
    while time.monotonic() < deadline:
        status = control_request(client)
        if status:
            check_browser_mode(args.browser, status)
            if status["phase"] == "ready":
                print("Open WebUI: " + WEBUI)
                if not args.browser and not args.no_browser:
                    os.startfile(WEBUI)
                return
            if status["phase"] == "starting" and last_component != status["component"]:
                last_component = status["component"]
                print("Starting " + last_component + "...", flush=True)
            if status["phase"] in ("failed", "stopping"):
                # Wait for ownership rollback before reporting this launch's failure.
                if status["launch"] != launch_id:
                    raise RuntimeError(status["error"] or "ForSLM is stopping")
        if not status and (child is None or child.poll() is not None) and not manager_exists():
            with suppress(OSError, ValueError, KeyError):
                result = read_json(CONTROL)
                if result["error"]:
                    raise RuntimeError(result["error"])
                if result["phase"] == "stopped":
                    raise RuntimeError("ForSLM stopped during startup")
            raise RuntimeError("ForSLM could not start. See logs/manager.log")
        time.sleep(0.3)
    raise TimeoutError("Service startup timed out. See logs/manager.log")


def stop(client):
    deadline = time.monotonic() + 150
    signaled = False
    while True:
        if not signaled:
            signaled = bool(control_request(client, "POST", "/stop"))
        if not manager_exists():
            if signaled:
                print("ForSLM stopped")
            return
        if time.monotonic() >= deadline:
            raise TimeoutError("ForSLM did not stop in time. See logs/manager.log")
        time.sleep(0.25)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("start", "run", "stop", "validate"))
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--browser")
    parser.add_argument("--launch")
    args = parser.parse_args()
    if args.browser:
        if args.no_browser:
            parser.error("--browser and --no-browser cannot be combined")
        args.browser = Browser.resolve(args.browser)
    RUNTIME.mkdir(exist_ok=True)
    LOGS.mkdir(exist_ok=True)
    (ROOT / "data").mkdir(exist_ok=True)
    if args.command == "run":
        with LOCK.open("a+b") as file:
            if acquire(file):
                logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
                logging.getLogger("aiohttp.access").setLevel(logging.WARNING)
                with Job() as job:
                    asyncio.run(Manager(args, job).run())
    elif args.command == "validate":
        resolve(read_local())
        verify_database()
    else:
        with httpx.Client(timeout=2, trust_env=False) as client:
            launch(args, client) if args.command == "start" else stop(client)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        sys.exit(str(error))
