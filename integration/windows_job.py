"""Launch Windows process trees with kill-on-close ownership."""
import ctypes
from ctypes import wintypes
import subprocess
import time


class BasicLimits(ctypes.Structure):
    _fields_ = [
        ("process_time", ctypes.c_longlong), ("job_time", ctypes.c_longlong),
        ("flags", wintypes.DWORD), ("min_working_set", ctypes.c_size_t),
        ("max_working_set", ctypes.c_size_t), ("active_process_limit", wintypes.DWORD),
        ("affinity", ctypes.c_size_t), ("priority", wintypes.DWORD), ("scheduling", wintypes.DWORD),
    ]


class ExtendedLimits(ctypes.Structure):
    _fields_ = [("basic", BasicLimits), ("io", ctypes.c_ulonglong * 6),
                ("memory", ctypes.c_size_t * 4)]


class Accounting(ctypes.Structure):
    _fields_ = [("times", ctypes.c_longlong * 4), ("page_faults", wintypes.DWORD),
                ("total", wintypes.DWORD), ("active", wintypes.DWORD), ("terminated", wintypes.DWORD)]


kernel = ctypes.WinDLL("kernel32", use_last_error=True)


def bind(name, result, *args):
    function = getattr(kernel, name)
    function.restype = result
    function.argtypes = args
    return function


create_job = bind("CreateJobObjectW", wintypes.HANDLE, ctypes.c_void_p, wintypes.LPCWSTR)
set_job = bind("SetInformationJobObject", wintypes.BOOL, wintypes.HANDLE, ctypes.c_int,
               ctypes.c_void_p, wintypes.DWORD)
query_job = bind("QueryInformationJobObject", wintypes.BOOL, wintypes.HANDLE, ctypes.c_int,
                 ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p)
assign_job = bind("AssignProcessToJobObject", wintypes.BOOL, wintypes.HANDLE, wintypes.HANDLE)
terminate_job = bind("TerminateJobObject", wintypes.BOOL, wintypes.HANDLE, wintypes.UINT)
open_process = bind("OpenProcess", wintypes.HANDLE, wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
is_process_in_job = bind("IsProcessInJob", wintypes.BOOL, wintypes.HANDLE, wintypes.HANDLE,
                         ctypes.POINTER(wintypes.BOOL))
close_handle = bind("CloseHandle", wintypes.BOOL, wintypes.HANDLE)
resume_process = ctypes.WinDLL("ntdll").NtResumeProcess
resume_process.argtypes = (wintypes.HANDLE,)
resume_process.restype = ctypes.c_long


class Job:
    def __enter__(self):
        self.handle = create_job(None, None)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        limits = ExtendedLimits()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not set_job(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            error = ctypes.WinError(ctypes.get_last_error())
            close_handle(self.handle)
            raise error
        return self

    def spawn(self, args, *, flags=subprocess.CREATE_NO_WINDOW, **kwargs):
        # Assign the suspended process before it can create a child.
        process = subprocess.Popen(args, creationflags=flags | 0x00000004, **kwargs)
        try:
            if not assign_job(self.handle, int(process._handle)):
                raise ctypes.WinError(ctypes.get_last_error())
            status = resume_process(int(process._handle))
            if status < 0:
                raise OSError(f"Could not resume process (NTSTATUS {status:#x})")
        except BaseException:
            process.kill()
            process.wait()
            if process.stdout:
                process.stdout.close()
            raise
        return process

    def contains(self, pid):
        process = open_process(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not process:
            error = ctypes.get_last_error()
            if error in (5, 87):  # protected process or a process that has already exited
                return False
            raise ctypes.WinError(error)
        try:
            member = wintypes.BOOL()
            if not is_process_in_job(process, self.handle, ctypes.byref(member)):
                raise ctypes.WinError(ctypes.get_last_error())
            return bool(member.value)
        finally:
            close_handle(process)

    def active_count(self):
        info = Accounting()
        if not query_job(self.handle, 1, ctypes.byref(info), ctypes.sizeof(info), None):
            raise ctypes.WinError(ctypes.get_last_error())
        return info.active

    def terminate(self, timeout=8):
        if not terminate_job(self.handle, 1):
            raise ctypes.WinError(ctypes.get_last_error())
        deadline = time.monotonic() + timeout
        while True:
            active = self.active_count()
            if not active:
                return
            if time.monotonic() >= deadline:
                raise TimeoutError(f"{active} processes are still exiting")
            time.sleep(0.05)

    def __exit__(self, *_):
        close_handle(self.handle)
