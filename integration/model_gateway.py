"""llama.cpp lifecycle and HTTP forwarding for the two model endpoints."""
import asyncio
import json
from dataclasses import dataclass
import logging
from pathlib import Path
from urllib.parse import urlencode

from aiohttp import ClientError, ClientTimeout, web
from multidict import CIMultiDict
from yarl import URL

from runtime_config import RUNTIME

log = logging.getLogger("ForSLM")
HOP_HEADERS = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
               "te", "trailer", "transfer-encoding", "upgrade"}


def headers_for(headers, *omit):
    skip = HOP_HEADERS.union(omit)
    for value in headers.getall("Connection", []):
        skip.update(item.strip().lower() for item in value.split(","))
    return CIMultiDict((key, value) for key, value in headers.items() if key.lower() not in skip)


def unavailable(reason):
    return web.json_response({"error": {"message": reason, "type": "service_unavailable"}},
                             status=503, headers={"Retry-After": "2"})


@dataclass
class Route:
    name: str
    upstream: str
    lazy: bool
    uncertain: bool = False

    async def state(self, client):
        async with client.get(self.upstream + "/models", timeout=ClientTimeout(total=3)) as response:
            response.raise_for_status()
            entries = (await response.json())["data"]
        if len(entries) != 1 or entries[0]["id"] != self.name:
            raise ValueError(f"expected only {self.name} on the private router")
        status = entries[0]["status"]
        if status.get("failed") and status.get("value") != "unloaded":
            raise ValueError(f"{self.name} model process exited with code {status.get('exit_code')}")
        return status["value"]

    async def idle(self, client):
        if self.lazy:
            state = await self.state(client)
            if state in ("unloaded", "sleeping"):
                return True
            if state != "loaded":
                return False
        async with client.get(self.upstream + "/slots", params={"model": self.name, "autoload": "false"},
                              timeout=ClientTimeout(total=2)) as response:
            response.raise_for_status()
            slots = await response.json()
        return not any(slot["is_processing"] for slot in slots)

    async def wait_for_cleanup(self, client):
        try:
            async with asyncio.timeout(4):
                # Allow the router to observe the closed inference connection.
                await asyncio.sleep(0.25)
                while not await self.idle(client):
                    await asyncio.sleep(0.25)
        except (ClientError, TimeoutError, ValueError, KeyError, TypeError) as error:
            log.info("%s: could not confirm inference cleanup: %s", self.name, error or "deadline elapsed")

    async def unload(self, client):
        try:
            if await self.state(client) != "unloaded":
                async with client.post(self.upstream + "/models/unload", json={"model": self.name},
                                       timeout=ClientTimeout(total=10)) as response:
                    response.raise_for_status()
                async with asyncio.timeout(25):
                    while await self.state(client) != "unloaded":
                        await asyncio.sleep(0.25)
            log.info("%s: unloaded", self.name)
        except (ClientError, TimeoutError, ValueError, KeyError, TypeError) as error:
            self.uncertain = True
            log.warning("%s: unload status could not be confirmed: %s", self.name, error)

    async def recover(self, client):
        if self.uncertain:
            try:
                self.uncertain = await self.state(client) not in ("loaded", "sleeping", "unloaded")
            except (ClientError, TimeoutError, ValueError, KeyError, TypeError):
                return False
            if not self.uncertain:
                log.info("%s: model state available again", self.name)
        return not self.uncertain


def model_options(settings, key, model_path):
    bge = key == "bge"
    options = {"model": Path(model_path).as_posix(), "alias": settings[f"{key}_name"],
               "ctx-size": settings["reranker_context" if bge else "qwen_context"],
               "n-gpu-layers": settings["gpu_layers"]}
    if settings.get("device"):
        options["device"] = settings["device"]
    if bge:
        options.update({"embedding": True, "pooling": "rank", "reranking": True, "no-webui": True})
    else:
        options.update({"flash-attn": "on", "cache-type-k": "q8_0", "cache-type-v": "q8_0", "jinja": True})
    return options


def model_command(settings, key, model_path, port, lazy):
    args = [settings["llama_server"], "--host", "127.0.0.1", "--port", str(port)]
    options = model_options(settings, key, model_path)
    if lazy:
        preset = RUNTIME / f"{key}-models.ini"
        preset.write_text("version = 1\n\n[" + settings[f"{key}_name"] + "]\n" +
                          "".join(f"{item} = {'true' if value is True else value}\n"
                                  for item, value in options.items()), encoding="utf-8")
        return args + ["--models-preset", str(preset), "--models-max", "1", "--offline"]
    for item, value in options.items():
        args.append("--" + item)
        if value is not True:
            args.append(str(value))
    return args


async def forward(manager, route, request):
    path = request.rel_url.raw_path
    task_id = None
    lease = None
    if path.startswith("/_forslm-task/"):
        parts = path.split("/", 3)
        if len(parts) != 4 or parts[2] not in manager.tasks:
            raise web.HTTPForbidden()
        lease, path = manager.tasks[parts[2]], "/" + parts[3]
        task_id = parts[2]
    normalized = "/" + request.rel_url.path.strip("/")
    if lease:
        normalized = "/" + normalized.split("/", 3)[-1]
    model_list = normalized in ("/models", "/v1/models")
    if normalized.startswith(("/models/", "/v1/models/")) or (
        model_list and (request.method not in ("GET", "HEAD", "OPTIONS") or "reload" in request.query)
    ):
        raise web.HTTPNotFound()
    while manager.unloading:
        await manager.unload_finished.wait()
    if lease and (manager.tasks.get(task_id) is not lease or lease.state != "running"):
        return unavailable("ForSLM task is no longer running")
    if manager.stopping and lease is None:
        return unavailable("ForSLM is stopping")
    discovery = normalized in ("/health", "/v1/health", "/models", "/v1/models")
    current = asyncio.current_task()
    manager.model_requests.add(current)
    output = None
    forwarded = False
    try:
        if not discovery and not await route.recover(manager.client):
            return unavailable("Model state is temporarily unavailable")
        query = request.rel_url.raw_query_string
        headers = headers_for(request.headers, "host", "expect", "content-encoding")
        body = request.content if request.can_read_body else None
        if route.lazy and request.method == "POST" and request.content_type == "application/json":
            body = await request.read()
            try:
                data = json.loads(body)
                if isinstance(data, dict) and "model" not in data:
                    data["model"] = route.name
                    body = json.dumps(data, ensure_ascii=False).encode()
            except ValueError:
                pass  # Let llama.cpp produce its native validation response.
            headers.pop("Content-Length", None)
        elif "Content-Encoding" in request.headers:
            headers.pop("Content-Length", None)
        if route.lazy and request.method == "GET" and normalized in ("/slots", "/lora-adapters"):
            if "model" not in request.query:
                model_query = urlencode({"model": route.name})
                query = f"{query}&{model_query}" if query else model_query
        target = URL(route.upstream + path + (f"?{query}" if query else ""), encoded=True)
        forwarded = True
        async with manager.client.request(request.method, target, allow_redirects=False, data=body,
                                          headers=headers, skip_auto_headers={"Content-Type"}) as response:
            output = web.StreamResponse(status=response.status, reason=response.reason,
                                        headers=headers_for(response.headers))
            await output.prepare(request)
            async for chunk in response.content.iter_any():
                await output.write(chunk)
            await output.write_eof()
            return output
    except (asyncio.CancelledError, ConnectionResetError):
        if forwarded and not discovery:
            await route.wait_for_cleanup(manager.client)
        raise
    except (ClientError, TimeoutError) as error:
        if forwarded and not discovery:
            await route.wait_for_cleanup(manager.client)
        if output and output.prepared:
            if request.transport:
                request.transport.close()
            return output
        log.warning("%s upstream request failed: %s", route.name, error)
        return web.json_response({"error": {"message": "Model upstream request failed"}},
                                 status=502)
    finally:
        manager.model_requests.discard(current)
