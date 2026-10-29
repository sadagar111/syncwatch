"""Two small Windows-only fixes for mpv's picture (no-ops on other systems).

* ``disable_child_windows``: in mpv's "window" mode, mpv draws into its own child window
  inside SyncWatch's video area. Windows sends mouse clicks to the window under the
  mouse, which would be mpv's, so double-click (fullscreen) and the mouse wheel (volume)
  wouldn't reach SyncWatch. A *disabled* child window gets no mouse input: Windows passes
  it to the parent, SyncWatch's own window. mpv keeps drawing into it as before.
* ``set_border``: an OpenGL window that exactly covers the screen makes Windows hide
  every other window of the program behind it, here the overlay with subtitles and
  messages. A 1-pixel border in fullscreen avoids that (the fix Qt's documentation
  gives: "Fullscreen OpenGL based windows", Qt for Windows - Specific Issues).
"""
from __future__ import annotations

import sys

GWL_STYLE = -16
WS_BORDER = 0x00800000
SWP_FLAGS = 0x0001 | 0x0002 | 0x0004 | 0x0010 | 0x0020  # NOSIZE|NOMOVE|NOZORDER|NOACTIVATE|FRAMECHANGED

_user32 = None


def _api():
    """user32 with exact argument types (a private copy, so nothing else's ctypes setup changes)."""
    global _user32
    if _user32 is None:
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.WinDLL("user32")
        user32.EnumChildWindows.argtypes = [wintypes.HWND, ctypes.c_void_p, wintypes.LPARAM]
        user32.EnumChildWindows.restype = wintypes.BOOL
        user32.IsWindowEnabled.argtypes = [wintypes.HWND]
        user32.IsWindowEnabled.restype = wintypes.BOOL
        user32.EnableWindow.argtypes = [wintypes.HWND, wintypes.BOOL]
        user32.EnableWindow.restype = wintypes.BOOL
        user32.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
        user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
        user32.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
        user32.SetWindowLongPtrW.restype = ctypes.c_ssize_t
        user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                        ctypes.c_int, wintypes.UINT]
        user32.SetWindowPos.restype = wintypes.BOOL
        _user32 = user32
    return _user32


def disable_child_windows(parent_hwnd: int) -> int:
    """Disable every enabled child window of ``parent_hwnd``. Returns how many were changed."""
    if sys.platform != "win32" or not parent_hwnd:
        return 0
    import ctypes
    from ctypes import wintypes
    user32 = _api()
    children = []
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def collect(hwnd, _lparam):
        children.append(hwnd)
        return True

    callback = callback_type(collect)   # kept in a variable until EnumChildWindows returns
    user32.EnumChildWindows(parent_hwnd, ctypes.cast(callback, ctypes.c_void_p), 0)
    changed = 0
    for hwnd in children:
        if hwnd and user32.IsWindowEnabled(hwnd):
            user32.EnableWindow(hwnd, False)
            changed += 1
    return changed


def set_border(hwnd: int, on: bool) -> None:
    """Add or remove the thin WS_BORDER frame of a top-level window."""
    if sys.platform != "win32" or not hwnd:
        return
    user32 = _api()
    style = user32.GetWindowLongPtrW(hwnd, GWL_STYLE)
    wanted = style | WS_BORDER if on else style & ~WS_BORDER
    if wanted != style:
        user32.SetWindowLongPtrW(hwnd, GWL_STYLE, wanted)
        user32.SetWindowPos(hwnd, None, 0, 0, 0, 0, SWP_FLAGS)
