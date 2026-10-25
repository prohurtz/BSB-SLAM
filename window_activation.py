"""Restore this application's existing Tk window on a second launch."""
import ctypes
from ctypes import wintypes
import os
import time


def activate_existing_window(prefix="Beyond-SLAM ", user32=None):
    if os.name != "nt" and user32 is None:
        return False
    api = user32 or ctypes.WinDLL("user32", use_last_error=True)
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    api.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
    api.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    api.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    api.IsIconic.argtypes = [wintypes.HWND]
    api.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    api.SetForegroundWindow.argtypes = [wintypes.HWND]
    found = []
    def visit(hwnd, _):
        name, title = ctypes.create_unicode_buffer(256), ctypes.create_unicode_buffer(512)
        api.GetClassNameW(hwnd, name, len(name))
        api.GetWindowTextW(hwnd, title, len(title))
        if name.value == "TkTopLevel" and title.value.startswith(prefix):
            api.ShowWindow(hwnd, 9 if api.IsIconic(hwnd) else 5)
            api.SetForegroundWindow(hwnd)
            found.append(hwnd)
            return False
        return True
    api.EnumWindows(callback_type(visit), 0)
    return bool(found)


def restore_existing_instance():
    # A second launch can arrive while the first process is still creating Tk.
    for _ in range(30):
        if activate_existing_window():
            return True
        time.sleep(.1)
    return False
