"""Read and maintain the local Pipe through the Open WebUI Functions API."""
from contextlib import suppress
import os
import shutil

from runtime_config import DEFAULT_URLS, LEGACY_VALVES, ROOT, endpoint, local_file

FUNCTION = "/api/v1/functions/id/local_ai_search"


async def sign_in(client, url):
    async with client.post(url + "/api/v1/auths/signin",
                           json={"email": "admin@localhost", "password": "admin"}) as response:
        response.raise_for_status()
        return {"Authorization": "Bearer " + (await response.json())["token"]}


async def inspect_pipe(client, url, auth):
    async with client.get(url + FUNCTION, headers=auth) as response:
        if response.status in (401, 404) and (await response.json()).get("detail") in ("Not Found", "We could not find what you're looking for :/"):
            return None, {}
        response.raise_for_status()
        existing = await response.json()
    async with client.get(url + FUNCTION + "/valves", headers=auth) as response:
        response.raise_for_status()
        return existing, await response.json() or {}


def migrate_valves(data, valves, log):
    migrated = dict(data)
    for valve, key in LEGACY_VALVES.items():
        value = valves.get(valve)
        if not value:
            continue
        if key in DEFAULT_URLS:
            with suppress(ValueError):
                value = endpoint(value, key)
                if value == DEFAULT_URLS[key]:
                    continue
        elif os.path.normcase(value) in ("node", os.path.normcase(shutil.which("node") or "")):
            continue
        if key in data:
            if data[key] != value:
                log.warning("%s in config/local.json takes precedence over %s", key, valve)
            continue
        try:
            migrated[key] = endpoint(value, key) if key in DEFAULT_URLS else local_file(value, key)
        except ValueError:
            if key != "node_path":
                raise
            log.warning("Ignoring unavailable legacy Node_Path %s", value)
    return migrated


async def register(client, url, auth, existing, model):
    code = (ROOT / "integration" / "local_search_pipe.py").read_text(encoding="utf-8")
    form = {"id": "local_ai_search", "name": model + " · 联网搜索", "content": code,
            "meta": {"description": "联网搜索 → BGE 重排 → Qwen 回答与引用"}}
    if existing is None or existing["content"] != code or existing["name"] != form["name"]:
        path = "/api/v1/functions/create" if existing is None else FUNCTION + "/update"
        async with client.post(url + path, json=form, headers=auth) as response:
            response.raise_for_status()
            existing = await response.json()
    if not existing["is_active"]:
        async with client.post(url + FUNCTION + "/toggle", headers=auth) as response:
            response.raise_for_status()
            if not (await response.json())["is_active"]:
                raise RuntimeError("The search Pipe could not be enabled")
