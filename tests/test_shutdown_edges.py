"""Regression tests for startup reuse and per-model unload attempts."""
import asyncio
from collections import deque
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from aiohttp import ClientSession, web
from aiohttp.test_utils import TestServer

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "integration"))
from model_gateway import Route
import service_manager as service


class StartupModeTests(unittest.TestCase):
    def test_manual_modes_cannot_reuse_managed_instance(self):
        status = {"phase": "ready", "browser": {"path": "chrome.exe", "profile": "owned-profile"}}
        for no_browser in (True, False):
            with (
                self.subTest(no_browser=no_browser),
                patch.object(service, "control_request", return_value=status),
                patch.object(service.os, "startfile") as open_page,
            ):
                with self.assertRaisesRegex(RuntimeError, "Stop.cmd"):
                    service.launch(SimpleNamespace(browser=None, no_browser=no_browser), None)
                open_page.assert_not_called()

    def test_only_the_same_executable_and_profile_match_managed_mode(self):
        browser = SimpleNamespace(exe=Path("firefox.exe"), profile=Path("release-profile"))
        service.check_browser_mode(None, {"browser": None})
        service.check_browser_mode(browser, {"browser": {"path": "firefox.exe", "profile": "release-profile"}})
        for bound in (None, {"path": "firefox-esr.exe", "profile": "release-profile"},
                      {"path": "firefox.exe", "profile": "other-profile"}):
            with self.subTest(bound=bound), self.assertRaisesRegex(RuntimeError, "Stop.cmd"):
                service.check_browser_mode(browser, {"browser": bound})


class Router:
    def __init__(self, name, clock):
        self.name, self.clock = name, clock
        self.outcomes = deque()
        self.posts = []
        self.states = []
        self.model_state = "loaded"
        self.busy = False
        self.app = web.Application()
        self.app.router.add_get("/models", self.models)
        self.app.router.add_get("/slots", self.slots)
        self.app.router.add_post("/models/unload", self.unload)

    async def models(self, request):
        self.states.append(self.clock())
        return web.json_response({"data": [{"id": self.name, "status": {"value": self.model_state}}]})

    async def slots(self, request):
        return web.json_response([{"is_processing": self.busy}])

    async def unload(self, request):
        self.posts.append(self.clock())
        if self.outcomes:
            outcome = self.outcomes.popleft()
            if outcome == "lost":
                self.model_state = "unloaded"
                request.transport.abort()
                return web.Response()
            return web.Response(status=outcome)
        self.model_state = "unloaded"
        return web.json_response({})


class UnloadRetryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.now = 0
        self.page = {"count": 0, "generation": 1, "empty_seconds": 30}
        self.manager = service.Manager(SimpleNamespace(browser=None), None)
        self.manager.control_port = 0
        self.manager.settings["unload_grace_seconds"] = 30
        self.manager.json = AsyncMock(side_effect=lambda *_args, **_kwargs: self.page.copy())
        self.manager.client = await self.enterAsyncContext(ClientSession())
        self.qwen = await self.start_router("qwen")
        self.bge = await self.start_router("bge")

    async def start_router(self, name):
        router = Router(name, lambda: self.now)
        server = await self.enterAsyncContext(TestServer(router.app))
        self.manager.routes[name] = Route(name, str(server.make_url("/")).rstrip("/"), lazy=True)
        return router

    async def watch_until(self, end, on_tick=lambda _: None):
        async def tick(seconds):
            self.now += seconds
            on_tick(self.now)
            if self.now > end:
                self.manager.request_stop("test")

        with (
            patch.object(service, "time", SimpleNamespace(monotonic=lambda: self.now)),
            patch.object(service, "asyncio", SimpleNamespace(sleep=tick, gather=asyncio.gather)),
            self.assertLogs("ForSLM", "INFO") as logs,
        ):
            await self.manager.watch_pages()
        return "\n".join(logs.output)

    async def test_retries_only_the_failed_model_and_defers_for_work(self):
        self.bge.outcomes.extend([503, 503])

        def work(now):
            self.assertFalse(self.manager.unloading)
            self.manager.tasks = {"question": object()} if 2 <= now < 3 else {}
            self.manager.model_requests = {object()} if 3 <= now < 4 else set()
            self.bge.busy = 4 <= now < 5

        logs = await self.watch_until(11, work)
        self.assertEqual(self.qwen.posts, [0.5])
        self.assertEqual(self.bge.posts, [0.5, 5, 10])
        self.assertEqual(self.bge.model_state, "unloaded")
        self.assertFalse(self.manager.routes["bge"].uncertain)
        self.assertIn("retry in 2 seconds", logs)
        self.assertIn("retry in 5 seconds", logs)

    async def test_lost_response_is_confirmed_without_another_unload(self):
        self.bge.outcomes.append("lost")
        await self.watch_until(4)
        self.assertEqual(self.bge.posts, [0.5])
        self.assertIn(2.5, self.bge.states)
        self.assertEqual(self.bge.model_state, "unloaded")
        self.assertFalse(self.manager.routes["bge"].uncertain)

    async def test_reconnection_cancels_retries_until_the_next_last_page_close(self):
        self.bge.outcomes.append(503)

        def reconnect(now):
            if now == 1:
                self.page.update(count=1, generation=2, empty_seconds=None)
            if now == 4:
                self.page.update(count=0, generation=3, empty_seconds=30)

        await self.watch_until(6, reconnect)
        self.assertEqual(self.bge.posts, [0.5, 4])
        self.assertEqual(self.qwen.posts, [0.5])

    async def test_exhaustion_leaves_the_model_available_and_stops_retrying(self):
        self.bge.outcomes.extend([503, 503, 502])
        logs = await self.watch_until(12)
        self.assertEqual(self.bge.posts, [0.5, 2.5, 7.5])
        self.assertEqual(self.bge.model_state, "loaded")
        self.assertIn("bge: unload not confirmed after 3 attempts", logs)
        self.assertIn("502", logs)
        self.assertTrue(self.manager.unload_finished.is_set())
        self.assertTrue(await self.manager.routes["bge"].recover(self.manager.client))

    async def test_new_request_invalidates_a_pending_idle_check(self):
        checks = 0

        async def presence(*_args, **_kwargs):
            nonlocal checks
            checks += 1
            if checks == 2:
                self.manager.model_admissions += 1
            return self.page.copy()

        self.manager.json.side_effect = presence
        await self.watch_until(2)
        self.assertEqual(self.qwen.posts, [1])
        self.assertEqual(self.bge.posts, [1])

    async def test_service_stop_takes_priority_over_a_pending_retry(self):
        self.bge.outcomes.append(503)
        await self.watch_until(1)
        self.assertEqual(self.bge.posts, [0.5])
        self.assertEqual(self.bge.model_state, "loaded")
