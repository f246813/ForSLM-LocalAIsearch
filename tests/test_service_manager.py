"""Regression tests for provider configuration and model admission."""
import asyncio
import logging
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest

from aiohttp.test_utils import make_mocked_request

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "integration"))
from model_gateway import forward
from service_manager import DEFAULT_URLS, plan_webui_models, pipe


class WebUIProviderTests(unittest.TestCase):
    def test_project_connection_follows_config_changes_and_returns_to_default(self):
        default = DEFAULT_URLS["qwen_url"]
        independently_configured = "http://localhost:9090/v1"
        config = {
            "OPENAI_API_BASE_URLS": [independently_configured, default],
            "OPENAI_API_KEYS": ["user-saved-key", "local-llama"],
            "OPENAI_API_CONFIGS": {independently_configured: {"enable": False, "prefix_id": "saved"}},
        }
        for new_url in ("http://localhost:8085/v1", "http://localhost:8086/v1", default):
            urls, entries = plan_webui_models(config, new_url)
            self.assertEqual(urls, [independently_configured, new_url])
            self.assertEqual(entries["0"], {"enable": False, "prefix_id": "saved"})
            config["OPENAI_API_BASE_URLS"] = urls
            config["OPENAI_API_CONFIGS"] = entries

    def test_saved_user_key_keeps_connection_at_its_configured_endpoint(self):
        old = DEFAULT_URLS["qwen_url"]
        config = {"OPENAI_API_BASE_URLS": [old], "OPENAI_API_KEYS": ["user-saved-key"]}
        urls, entries = plan_webui_models(config, "https://model.example/v1")
        self.assertEqual(urls, [old])
        self.assertEqual(entries, {"0": {}})

    def test_explicit_run_config_wins_over_invalid_saved_valve(self):
        data = {"qwen_url": "http://localhost:8085/v1"}
        with self.assertLogs("ForSLM"):
            migrated = pipe.migrate_valves(data, {"Qwen_URL": "invalid-old-address"},
                                          logging.getLogger("ForSLM"))
        self.assertEqual(migrated, data)


class ModelAdmissionTests(unittest.IsolatedAsyncioTestCase):
    async def test_shutdown_does_not_accept_requests_from_a_canceling_task(self):
        manager = SimpleNamespace(tasks={"task": SimpleNamespace(state="canceling")},
                                  unloading=False, stopping=True, model_requests=set())
        request = make_mocked_request("POST", "/_forslm-task/task/v1/chat/completions")
        response = await forward(manager, None, request)
        self.assertEqual(response.status, 503)
        self.assertFalse(manager.model_requests)

    async def test_waiting_request_rechecks_lease_after_unload(self):
        finished = asyncio.Event()
        manager = SimpleNamespace(tasks={"task": SimpleNamespace(state="running")},
                                  unloading=True, unload_finished=finished,
                                  stopping=False, model_requests=set())
        request = make_mocked_request("POST", "/_forslm-task/task/v1/chat/completions")
        queued = asyncio.create_task(forward(manager, None, request))
        await asyncio.sleep(0)
        self.assertFalse(queued.done())
        manager.tasks.clear()
        manager.unloading = False
        finished.set()
        self.assertEqual((await queued).status, 503)
        self.assertFalse(manager.model_requests)
