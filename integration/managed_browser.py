"""Bind and observe one isolated Windows browser session."""
import asyncio
from contextlib import suppress
import ctypes
from ctypes import wintypes
from dataclasses import dataclass
import hashlib
import logging
import os
from pathlib import Path
import subprocess
import time
from uuid import uuid4
import winreg

import psutil

from browser_window import has_browser_chrome
from runtime_config import RUNTIME, local_file
from windows_job import Job, bind, close_handle

log = logging.getLogger("ForSLM")
USER = ctypes.WinDLL("user32", use_last_error=True)
SHLWAPI = ctypes.WinDLL("shlwapi", use_last_error=True)
ENUM_CALLBACK = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
HWND_MESSAGE = -3
GW_OWNER = 4
GWL_STYLE, GWL_EXSTYLE = -16, -20
WS_CHILD, WS_SYSMENU, WS_MINIMIZEBOX = 0x40000000, 0x80000, 0x20000
WS_EX_DLGMODALFRAME, WS_EX_TOOLWINDOW, WS_EX_NOACTIVATE = 0x1, 0x80, 0x08000000
POLL_SECONDS = 0.25
WINDOW_GRACE = 1
STARTUP_TIMEOUT = 20
EXIT_TIMEOUT = 4


def api(library, name, result, *args):
    function = getattr(library, name)
    function.restype = result
    function.argtypes = args
    return function


enum_windows = api(USER, "EnumWindows", wintypes.BOOL, ENUM_CALLBACK, wintypes.LPARAM)
window_ids = api(USER, "GetWindowThreadProcessId", wintypes.DWORD, wintypes.HWND, ctypes.POINTER(wintypes.DWORD))
window_class = api(USER, "GetClassNameW", ctypes.c_int, wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
window_style = api(USER, "GetWindowLongW", wintypes.LONG, wintypes.HWND, ctypes.c_int)
window_visible = api(USER, "IsWindowVisible", wintypes.BOOL, wintypes.HWND)
window_relative = api(USER, "GetWindow", wintypes.HWND, wintypes.HWND, wintypes.UINT)
find_window = api(USER, "FindWindowExW", wintypes.HWND, wintypes.HWND, wintypes.HWND,
                  wintypes.LPCWSTR, wintypes.LPCWSTR)
post_message = api(USER, "PostMessageW", wintypes.BOOL, wintypes.HWND, wintypes.UINT,
                   wintypes.WPARAM, wintypes.LPARAM)
assoc_query = api(SHLWAPI, "AssocQueryStringW", ctypes.c_long, wintypes.DWORD, wintypes.DWORD,
                  wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD))
open_file = bind("CreateFileW", wintypes.HANDLE, wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                 ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE)


def default_browser():
    size = wintypes.DWORD()
    flags, executable = 0x1000, 2  # ASSOCF_IS_PROTOCOL, ASSOCSTR_EXECUTABLE
    assoc_query(flags, executable, "http", "open", None, ctypes.byref(size))
    if size.value:
        path = ctypes.create_unicode_buffer(size.value)
        result = assoc_query(flags, executable, "http", "open", path, ctypes.byref(size))
        if result == 0 and path.value:
            return path.value
    raise ValueError("Windows HTTP association does not resolve to a browser executable. Specify its path.")


def path_key(path):
    return os.path.normcase(os.path.realpath(path))


def window_owner(hwnd):
    pid = wintypes.DWORD()
    thread = window_ids(hwnd, ctypes.byref(pid))
    return pid.value, thread


def chromium_profile_pid(profile):
    hwnd = find_window(HWND_MESSAGE, None, "Chrome_MessageWindow", str(profile))
    return window_owner(hwnd)[0] if hwnd else 0


def file_locked(path):
    handle = open_file(str(path), 0xC0000000, 7, None, 3, 0, None)  # OPEN_EXISTING, read/write
    if handle != ctypes.c_void_p(-1).value:
        close_handle(handle)
        return False
    error = ctypes.get_last_error()
    if error in (2, 3):
        return False
    if error in (32, 33):
        return True
    raise ctypes.WinError(error)


def check_directory_policy(engine):
    vendor = {"msedge": "Microsoft\\Edge", "chrome": "Google\\Chrome"}.get(engine)
    if not vendor:
        return
    key = "Software\\Policies\\" + vendor
    for root in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
            try:
                with winreg.OpenKey(root, key, access=winreg.KEY_QUERY_VALUE | view) as handle:
                    value, _ = winreg.QueryValueEx(handle, "UserDataDir")
            except FileNotFoundError:
                continue
            if value:
                raise ValueError(f"{engine}: UserDataDir policy overrides the independent profile ({key})")


@dataclass(frozen=True)
class Browser:
    exe: Path
    profile: Path
    window_class: str

    @classmethod
    def resolve(cls, value):
        path = Path(local_file(default_browser() if value.casefold() == "auto" else value, "ManagedBrowser"))
        directory = f"browser-{path.stem.lower()}"
        match path.name.lower():
            case "firefox.exe":
                key = hashlib.sha256(path_key(path).encode("utf-8")).hexdigest()[:16]
                directory += "-" + key
                native_class = "MozillaWindowClass"
            case "chrome.exe" | "msedge.exe":
                native_class = "Chrome_WidgetWin_1"
            case _:
                raise ValueError(f"ManagedBrowser does not support {path}. Specify Edge, Chrome or Firefox.")
        profile = (RUNTIME / directory).resolve()
        if profile == RUNTIME.resolve() or not profile.is_relative_to(RUNTIME.resolve()):
            raise ValueError(f"Project profile must be inside {RUNTIME}: {profile}")
        return cls(path, profile, native_class)

    @property
    def firefox(self):
        return self.exe.name.lower() == "firefox.exe"

    @property
    def lockfile(self):
        return self.profile / ("parent.lock" if self.firefox else "lockfile")

    def preflight(self):
        check_directory_policy(self.exe.stem.lower())
        if file_locked(self.lockfile) or (not self.firefox and chromium_profile_pid(self.profile)):
            raise ValueError(f"Browser profile is already in use: {self.profile}")

    def command(self, url):
        if self.firefox:
            return [str(self.exe), "--new-instance", "--profile", str(self.profile), "--new-window", url]
        return [str(self.exe), f"--user-data-dir={self.profile}", "--disable-background-mode",
                "--no-first-run", "--no-default-browser-check", "--new-window", url]

    def uses_profile(self, process):
        if not self.firefox:
            return chromium_profile_pid(self.profile) == process.pid
        args = process.cmdline()
        return any(arg.lower() in ("--profile", "-profile") and path_key(path) == path_key(self.profile)
                   for arg, path in zip(args, args[1:])) and file_locked(self.lockfile)

    def is_main_window(self, hwnd):
        if not window_visible(hwnd) or window_relative(hwnd, GW_OWNER):
            return False
        name = ctypes.create_unicode_buffer(128)
        if not window_class(hwnd, name, len(name)) or name.value != self.window_class:
            return False
        style = window_style(hwnd, GWL_STYLE)
        extended = window_style(hwnd, GWL_EXSTYLE)
        return bool(style & WS_SYSMENU and style & WS_MINIMIZEBOX and not style & WS_CHILD
                    and not extended & (WS_EX_DLGMODALFRAME | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE))


@dataclass(frozen=True)
class Window:
    hwnd: int
    pid: int
    thread: int
    birth: float


def window_identity(hwnd):
    pid, thread = window_owner(hwnd)
    if thread:
        with suppress(psutil.NoSuchProcess):
            return Window(hwnd, pid, thread, psutil.Process(pid).create_time())
    return None


def scan_windows(browser, job, known):
    candidates = set()

    @ENUM_CALLBACK
    def visit(hwnd, _):
        if browser.is_main_window(hwnd):
            candidates.add(hwnd)
        return True

    ctypes.set_last_error(0)
    if not enum_windows(visit, 0):
        error = ctypes.get_last_error()
        raise ctypes.WinError(error) if error else OSError("EnumWindows failed")

    windows = {window for window in known if window_identity(window.hwnd) == window}
    known_handles = {window.hwnd for window in windows}
    owners = {}
    for hwnd in candidates - known_handles:
        pid, thread = window_owner(hwnd)
        if not thread:
            continue
        if pid not in owners:
            owners[pid] = None
            if job.contains(pid):
                with suppress(psutil.NoSuchProcess):
                    process = psutil.Process(pid)
                    if path_key(process.exe()) == path_key(browser.exe) and browser.uses_profile(process):
                        owners[pid] = process.create_time()
        if owners[pid] is None or not has_browser_chrome(hwnd, browser.firefox):
            continue
        if window_owner(hwnd) == (pid, thread):
            windows.add(Window(hwnd, pid, thread, owners[pid]))
    return windows


class BrowserSession:
    def __init__(self, browser):
        self.browser = browser
        self.id = uuid4().hex
        self.windows = set()
        self.phase = "starting"
        self.error = None
        self.job = None
        self.launcher = None
        self.bound = False

    def state(self):
        return {"id": self.id, "path": str(self.browser.exe), "profile": str(self.browser.profile),
                "phase": self.phase, "error": self.error,
                "windows": sorted(window.hwnd for window in self.windows),
                "window_pids": sorted({window.pid for window in self.windows})}

    async def read_windows(self):
        worker = asyncio.create_task(asyncio.to_thread(scan_windows, self.browser, self.job, self.windows))
        try:
            return await asyncio.shield(worker)
        except asyncio.CancelledError:
            # The scan must release its native handles before the browser Job Object closes.
            await asyncio.gather(worker, return_exceptions=True)
            raise

    async def scan(self):
        try:
            windows = await self.read_windows()
        except (OSError, psutil.Error) as error:
            detail = str(error)
            if self.error != detail:
                log.warning("Browser %s window observation unavailable: %s", self.id, detail)
            self.phase, self.error = "unknown", detail
            return None
        if self.error:
            log.info("Browser %s window observation recovered", self.id)
        if self.windows != windows:
            log.info("Browser %s windows %s (PIDs %s)", self.id, sorted(w.hwnd for w in windows),
                     sorted({w.pid for w in windows}))
        self.windows = windows
        self.phase, self.error = ("watching" if self.bound or windows else "starting"), None
        return windows

    async def start(self, url, logs):
        self.browser.preflight()
        self.browser.profile.mkdir(parents=True, exist_ok=True)
        self.job = Job().__enter__()
        with (logs / "browser.out.log").open("ab") as stdout, (logs / "browser.err.log").open("ab") as stderr:
            launcher = self.launcher = self.job.spawn(self.browser.command(url), flags=0, cwd=self.browser.exe.parent,
                                      stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr)
        log.info("Browser %s starting %s with profile %s (launcher PID %s)", self.id, self.browser.exe,
                 self.browser.profile, launcher.pid)
        deadline = time.monotonic() + STARTUP_TIMEOUT
        while time.monotonic() < deadline:
            if await self.scan():
                self.bound = True
                return
            # Firefox's GUI launcher may exit before its browser process shows a window.
            if launcher.poll() is not None and not self.job.active_count():
                break
            await asyncio.sleep(POLL_SECONDS)
        detail = self.error or "no main window matched the browser Job Object and independent profile"
        self.phase, self.error = "failed", detail
        raise RuntimeError(f"{self.browser.exe.name}: {detail}. See logs/browser.err.log")

    async def observe(self):
        empty_since = None
        while True:
            windows = await self.scan()
            if windows is None or windows:
                empty_since = None
            elif empty_since is None:
                empty_since = time.monotonic()
            elif time.monotonic() - empty_since >= WINDOW_GRACE:
                self.phase = "closed"
                return self.id
            await asyncio.sleep(POLL_SECONDS)

    async def close(self):
        if not self.job:
            if self.phase != "failed":
                self.phase = "closed"
            return
        try:
            if self.bound:
                if self.phase != "closed":
                    await self.scan()
                    for window in self.windows:
                        with suppress(psutil.Error):
                            if window_identity(window.hwnd) == window and self.job.contains(window.pid):
                                post_message(window.hwnd, 0x0010, 0, 0)  # WM_CLOSE
                deadline = time.monotonic() + EXIT_TIMEOUT
                while self.job.active_count() and time.monotonic() < deadline:
                    await asyncio.sleep(POLL_SECONDS)
            if self.job.active_count():
                log.info("Retiring remaining processes in browser Job Object %s", self.id)
                await asyncio.to_thread(self.job.terminate, EXIT_TIMEOUT)
            if self.launcher:
                self.launcher.wait(timeout=2)
                self.launcher = None
            self.windows.clear()
            if self.phase != "failed":
                self.phase = "closed"
        finally:
            self.job.__exit__(None, None, None)
            self.job = None
