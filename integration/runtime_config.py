"""Shared local settings and the resolved runtime snapshot."""
import json
from pathlib import Path
import re
import shutil
import time
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent.parent
LOCAL = ROOT / "config" / "local.json"
RUNTIME = ROOT / "runtime"
RESOLVED = RUNTIME / "resolved.json"
DEFAULT_URLS = {
    "qwen_url": "http://127.0.0.1:8080/v1",
    "bge_url": "http://127.0.0.1:8081/v1",
    "searxng_url": "http://127.0.0.1:8888",
}
LEGACY_VALVES = {"Qwen_URL": "qwen_url", "BGE_URL": "bge_url",
                 "SearXNG_URL": "searxng_url", "Node_Path": "node_path"}
MODELS = {"qwen": "Qwen3-4B", "bge": "bge-reranker-v2-m3"}


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write_json(path, data):
    # Replace the snapshot in one operation. Windows readers can briefly lock it.
    pending = path.with_suffix(".tmp")
    pending.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    for _ in range(20):
        try:
            pending.replace(path)
            return
        except PermissionError:
            time.sleep(0.05)
    pending.replace(path)


def endpoint(value, key):
    if not isinstance(value, str) or any(char.isspace() for char in value):
        raise ValueError(f"{key}: expected an HTTP(S) URL")
    value = value.rstrip("/")
    address = urlsplit(value)
    if (address.scheme not in ("http", "https") or not address.hostname or address.username
            or address.password or address.query or address.fragment or address.port == 0):
        raise ValueError(f"{key}: expected an HTTP(S) URL without credentials, query or fragment")
    if key == "bge_url" and "/v1" not in address.path:
        value += "/v1"
    return value


def local_file(value, key):
    if not isinstance(value, str) or not value:
        raise ValueError(f"{key}: expected a file path")
    path = (ROOT / value).resolve()
    if not path.is_file():
        raise ValueError(f"{key}: file not found: {path}")
    return str(path)


def integer(data, key, default, minimum, maximum=None):
    value = data.get(key, default)
    if type(value) is not int or value < minimum or (maximum is not None and value > maximum):
        raise ValueError(f"{key}: expected an integer between {minimum} and {maximum or 'the system limit'}")
    return value


def read_local():
    if not LOCAL.is_file():
        raise ValueError("Run Install.cmd, then edit config/local.json")
    data = read_json(LOCAL)
    if not isinstance(data, dict):
        raise ValueError("config/local.json must contain a JSON object")
    return data


def resolve(data, *, deferred_node=False):
    settings = dict(data)
    for key, default in DEFAULT_URLS.items():
        settings[key] = endpoint(data.get(key, default), key)
    if not deferred_node or "node_path" in data:
        settings["node_path"] = local_file(data.get("node_path", shutil.which("node")), "node_path")
    settings["gpu_layers"] = integer(data, "gpu_layers", 99, -1)
    settings["qwen_context"] = integer(data, "qwen_context", 32768, 1)
    settings["reranker_context"] = integer(data, "reranker_context", 8192, 1)
    settings["unload_grace_seconds"] = integer(data, "unload_grace_seconds", 30, 0, 3600)
    settings["task_timeout_seconds"] = integer(data, "task_timeout_seconds", 420, 1, 3600)
    settings["shutdown_grace_seconds"] = integer(data, "shutdown_grace_seconds", 15, 0, 120)
    for key, default in MODELS.items():
        name = data.get(f"{key}_name", default)
        if not isinstance(name, str) or not re.fullmatch(r"[\w./:@+-]+", name):
            raise ValueError(f"{key}_name: invalid model name")
        settings[f"{key}_name"] = name
    device = data.get("device")
    if device is not None and (not isinstance(device, str) or not re.fullmatch(r"[\w.,:-]+", device)):
        raise ValueError("device: expected a device name or comma-separated device names")
    return settings
