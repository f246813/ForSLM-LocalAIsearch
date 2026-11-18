"""Run the installed Open WebUI with a view of its authenticated page connections."""
import asyncio
from contextlib import suppress
import hmac
import logging
import time

import open_webui
import uvicorn

PRESENCE_PATH = "/_forslm/presence"
CHAT_PATHS = {"/api/chat/completions", "/api/v1/chat/completions"}


class CancelDisconnectedChat:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["path"] not in CHAT_PATHS or scope["method"] != "POST":
            await self.app(scope, receive, send)
            return

        data = asyncio.Queue(maxsize=1)
        response_complete = False

        async def watch():
            while True:
                event = await receive()
                if event["type"] == "http.disconnect":
                    return
                await data.put(event)

        async def read():
            if watcher.done() and data.empty():
                return {"type": "http.disconnect"}
            return await data.get()

        async def send_response(event):
            nonlocal response_complete
            await send(event)
            if event["type"] == "http.response.body" and not event.get("more_body"):
                response_complete = True

        watcher = asyncio.create_task(watch())
        request = asyncio.create_task(self.app(scope, read, send_response))
        try:
            completed, _ = await asyncio.wait((watcher, request), return_when=asyncio.FIRST_COMPLETED)
            if request in completed or response_complete:
                await request
            else:
                watcher.result()
        finally:
            for task in (request, watcher):
                if not task.done():
                    task.cancel()
                    with suppress(asyncio.CancelledError):
                        await task


def install_lifecycle(server):
    from fastapi import HTTPException, Request
    from open_webui.main import app
    from open_webui.socket.main import LOCAL_AUTHENTICATED_SIDS, connect, disconnect, sio, user_join

    from runtime_config import RESOLVED, read_json

    previous_count = generation = 0
    seen_page = False
    last_empty = None

    def track_page(handler):
        async def wrapped(*args, **kwargs):
            nonlocal previous_count, generation, seen_page, last_empty
            try:
                return await handler(*args, **kwargs)
            finally:
                count = len(LOCAL_AUTHENTICATED_SIDS)
                if count != previous_count:
                    generation += 1
                    previous_count = count
                if count:
                    seen_page = True
                    last_empty = None
                elif seen_page and last_empty is None:
                    last_empty = time.monotonic()
        return wrapped

    sio.on("connect", track_page(connect))
    sio.on("disconnect", track_page(disconnect))
    sio.on("user-join", track_page(user_join))
    app.add_middleware(CancelDisconnectedChat)

    def log_access(record):
        return not (isinstance(record.args, tuple) and len(record.args) == 5
                    and record.args[2] == PRESENCE_PATH and record.args[4] == 200)

    logging.getLogger("uvicorn.access").addFilter(log_access)

    def authenticate(request):
        try:
            token = read_json(RESOLVED)["control"]["token"]
        except (OSError, ValueError, KeyError):
            raise HTTPException(503) from None
        if not hmac.compare_digest(request.headers.get("X-ForSLM-Token", "").encode(), token.encode()):
            raise HTTPException(403)

    @app.get(PRESENCE_PATH, include_in_schema=False)
    async def presence(request: Request):
        authenticate(request)
        return {"count": len(LOCAL_AUTHENTICATED_SIDS), "seen": seen_page, "generation": generation,
                "empty_seconds": time.monotonic() - last_empty if last_empty is not None else None}

    @app.post("/_forslm/shutdown", include_in_schema=False)
    async def shutdown(request: Request):
        authenticate(request)
        server.should_exit = True
        return {"stopping": True}

    # The packaged frontend has a catch-all root mount.
    routes = app.router.routes
    routes[0:0] = routes[-2:]
    del routes[-2:]


if __name__ == "__main__":
    def run_with_lifecycle(*args, **kwargs):
        server = uvicorn.Server(uvicorn.Config(*args, timeout_graceful_shutdown=3, **kwargs))
        install_lifecycle(server)
        server.run()

    uvicorn.run = run_with_lifecycle
    open_webui.serve(host="127.0.0.1", port=3000)
