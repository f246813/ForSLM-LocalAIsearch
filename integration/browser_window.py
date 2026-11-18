"""Identify browser chrome without relying on localized window titles."""
from contextlib import contextmanager
import ctypes
from ctypes import wintypes
from uuid import UUID

ole = ctypes.OleDLL("ole32")
ole.CoInitializeEx.argtypes = (ctypes.c_void_p, wintypes.DWORD)
ole.CoCreateInstance.argtypes = (ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD,
                                 ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p))
automation = ctypes.WinDLL("oleaut32")
allocate_bstr = automation.SysAllocString
allocate_bstr.argtypes = (wintypes.LPCWSTR,)
allocate_bstr.restype = ctypes.c_void_p
free_bstr = automation.SysFreeString
free_bstr.argtypes = (ctypes.c_void_p,)
free_bstr.restype = None

CLSID_UI_AUTOMATION8 = UUID("e22ad333-b25f-460c-83d0-0581107395c9").bytes_le
IID_UI_AUTOMATION2 = UUID("34723aff-0c9d-49d0-9896-7ab52df8cd8a").bytes_le
INTERFACE_POINTER = ctypes.POINTER(ctypes.c_void_p)
AUTOMATION_ID = 30011
LEGACY_ACCESSIBLE_VALUE = 30093
TREE_ELEMENT_AND_CHILDREN = 3
TREE_DESCENDANTS = 4


class BstrVariant(ctypes.Structure):
    _fields_ = [("type", ctypes.c_ushort), ("reserved", ctypes.c_ushort * 3),
                ("value", ctypes.c_void_p), ("padding", ctypes.c_void_p)]


def method(interface, index, result, types, *args):
    vtable = ctypes.cast(interface, ctypes.POINTER(INTERFACE_POINTER)).contents
    invoke = ctypes.WINFUNCTYPE(result, ctypes.c_void_p, *types)(vtable[index])
    return invoke(interface, *args)


def checked(interface, index, types, *args):
    result = method(interface, index, ctypes.c_long, types, *args)
    if result < 0:
        raise OSError(f"Browser window type unavailable (UI Automation HRESULT {result & 0xffffffff:#010x})")


@contextmanager
def release(interface):
    try:
        yield
    finally:
        if interface:
            method(interface, 2, wintypes.ULONG, ())  # IUnknown::Release


def has_browser_chrome(hwnd, firefox):
    if firefox:
        # The MSAA value identifies the internal browser document in release and ESR.
        property_id, identifier = LEGACY_ACCESSIBLE_VALUE, "chrome://browser/content/browser.xhtml"
        scope = TREE_ELEMENT_AND_CHILDREN
    else:
        property_id, identifier = AUTOMATION_ID, "view_1000"  # VIEW_ID_TOOLBAR
        scope = TREE_DESCENDANTS
    ole.CoInitializeEx(None, 0)  # COINIT_MULTITHREADED
    try:
        client = ctypes.c_void_p()
        ole.CoCreateInstance(CLSID_UI_AUTOMATION8, None, 1, IID_UI_AUTOMATION2, ctypes.byref(client))
        with release(client):
            checked(client, 61, (wintypes.DWORD,), 250)  # IUIAutomation2::put_ConnectionTimeout
            checked(client, 63, (wintypes.DWORD,), 500)  # IUIAutomation2::put_TransactionTimeout
            root = ctypes.c_void_p()
            checked(client, 6, (wintypes.HWND, INTERFACE_POINTER), hwnd, ctypes.byref(root))
            with release(root):
                condition = ctypes.c_void_p()
                text = allocate_bstr(identifier)
                if not text:
                    raise OSError("Could not allocate the browser window identifier")
                try:
                    variant = BstrVariant(8, (ctypes.c_ushort * 3)(), text)  # VT_BSTR
                    checked(client, 23, (ctypes.c_int, BstrVariant, INTERFACE_POINTER),
                            property_id, variant, ctypes.byref(condition))
                finally:
                    free_bstr(text)
                with release(condition):
                    match = ctypes.c_void_p()
                    checked(root, 5, (ctypes.c_int, ctypes.c_void_p, INTERFACE_POINTER),
                            scope, condition, ctypes.byref(match))
                    with release(match):
                        return bool(match)
    finally:
        ole.CoUninitialize()
