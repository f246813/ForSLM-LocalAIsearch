"""Install/update a Pipe using Open WebUI's supported Functions API."""
import os
from pathlib import Path
import httpx

root = Path(os.getenv("FORSLM_PROJECT_ROOT", str(Path(__file__).resolve().parent.parent)))
with httpx.Client(base_url="http://127.0.0.1:3000", timeout=120, trust_env=False) as client:
    auth = client.post("/api/v1/auths/signin", json={"email": "admin@localhost", "password": "admin"})
    auth.raise_for_status()
    client.headers["Authorization"] = "Bearer " + auth.json()["token"]
    code = (root / "integration" / "local_search_pipe.py").read_text(encoding="utf-8")
    existing = client.get("/api/v1/functions/id/local_ai_search")
    form = {"id": "local_ai_search", "name": "Qwen3-4B · 联网搜索", "content": code,
            "meta": {"description": "联网搜索 → BGE 重排 → Qwen 回答与引用"}}
    if existing.status_code == 200:
        if existing.json()["content"] != code:
            response = client.post("/api/v1/functions/id/local_ai_search/update", json=form)
            response.raise_for_status()
        active = existing.json()["is_active"]
    else:
        response = client.post("/api/v1/functions/create", json=form)
        response.raise_for_status()
        active = response.json()["is_active"]
    if not active:
        response = client.post("/api/v1/functions/id/local_ai_search/toggle")
        response.raise_for_status()
    print("Search model enabled: local_ai_search")
