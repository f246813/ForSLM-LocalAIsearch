"""Windows regression tests for managed browser selection and shutdown."""
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "integration"))
import managed_browser as browser


class BrowserSelectionTests(unittest.TestCase):
    def test_auto_uses_the_same_firefox_adapter_and_profile_as_explicit_path(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            release = base / "release" / "firefox.exe"
            esr = base / "esr" / "firefox.exe"
            for executable in (release, esr):
                executable.parent.mkdir()
                executable.touch()
            with patch.object(browser, "RUNTIME", base / "runtime"):
                profiles = []
                for executable in (release, esr):
                    with patch.object(browser, "default_browser", return_value=str(executable)):
                        selected = browser.Browser.resolve("auto")
                    self.assertEqual(selected, browser.Browser.resolve(str(executable)))
                    self.assertEqual(selected.command("http://127.0.0.1:3000"), [
                        str(executable), "--new-instance", "--profile", str(selected.profile),
                        "--new-window", "http://127.0.0.1:3000",
                    ])
                    profiles.append(selected.profile)
                self.assertNotEqual(*profiles)

    def test_edge_policy_blocks_launch_before_profile_use(self):
        with patch.object(browser.winreg, "OpenKey") as opened, patch.object(
            browser.winreg, "QueryValueEx", return_value=(r"C:\Personal", browser.winreg.REG_SZ)
        ):
            edge = browser.Browser(Path("msedge.exe"), Path("project"), "Chrome_WidgetWin_1")
            with self.assertRaisesRegex(ValueError, "UserDataDir policy"):
                edge.preflight()
            self.assertIn(r"Policies\Microsoft\Edge", opened.call_args.args[1])


class BrowserObservationTests(unittest.IsolatedAsyncioTestCase):
    async def test_enumeration_failure_restarts_the_last_window_grace(self):
        target = browser.Browser(Path("firefox.exe"), Path("profile"), "MozillaWindowClass")
        session = browser.BrowserSession(target)
        session.bound = True
        existing = browser.Window(42, 1, 2, 3.0)
        scans = [
            {existing}, set(), OSError("EnumWindows failed"), set(),
            {existing}, set(), set(), set(),
        ]
        clock = iter((1, 2, 3, 3.4, 4.0))
        sleep = AsyncMock()
        with (
            patch.object(session, "read_windows", side_effect=scans) as scan,
            patch.object(browser, "asyncio", SimpleNamespace(sleep=sleep)),
            patch.object(browser, "time", SimpleNamespace(monotonic=lambda: next(clock))),
            self.assertLogs("ForSLM") as trace,
        ):
            closed = await session.observe()
        self.assertEqual(closed, session.id)
        self.assertEqual(scan.call_count, 8)
        self.assertEqual(sleep.await_count, 7)
        self.assertEqual(session.phase, "closed")
        self.assertIn("window observation unavailable", "\n".join(trace.output))
        self.assertIn("window observation recovered", "\n".join(trace.output))

    async def test_last_window_close_retires_background_processes(self):
        target = browser.Browser(Path("firefox.exe"), Path("profile"), "MozillaWindowClass")
        session = browser.BrowserSession(target)
        session.bound = True
        session.phase = "closed"
        session.job = browser.Job().__enter__()
        process = None
        try:
            process = session.job.spawn([sys.executable, "-c", "import time; time.sleep(30)"],
                                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.assertTrue(session.job.contains(process.pid))
            with patch.object(browser, "EXIT_TIMEOUT", 0.25), patch.object(browser, "POLL_SECONDS", 0.05):
                await session.close()
            self.assertIsNotNone(process.poll())
            self.assertEqual(session.phase, "closed")
        finally:
            if session.job:
                session.job.__exit__(None, None, None)
            if process:
                process.wait(timeout=3)

    def test_known_window_is_retained_when_no_longer_visible(self):
        target = browser.Browser(Path("firefox.exe"), Path("profile"), "MozillaWindowClass")
        window = browser.Window(42, 1, 2, 3.0)
        with patch.object(browser, "enum_windows", return_value=True), patch.object(
            browser, "window_identity", return_value=window
        ):
            self.assertEqual(browser.scan_windows(target, None, {window}), {window})
        with patch.object(browser, "enum_windows", return_value=True), patch.object(
            browser, "window_identity", return_value=browser.Window(42, 1, 2, 4.0)
        ):
            self.assertEqual(browser.scan_windows(target, None, {window}), set())


if __name__ == "__main__":
    unittest.main()
