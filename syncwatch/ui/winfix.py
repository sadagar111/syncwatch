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


def disable_child_windows(parent_hwnd: int) -> int:
    """Disable every enabled child window of ``parent_hwnd``. Returns how many were changed."""
    if sys.platform != "win32" or not parent_hwnd:
        return 0
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.windll.user32
    children = []
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def collect(hwnd, _lparam):
        children.append(hwnd)
        return True

    user32.EnumChildWindows(wintypes.HWND(parent_hwnd), callback_type(collect), 0)
    changed = 0
    for hwnd in children:
        if user32.IsWindowEnabled(hwnd):
            user32.EnableWindow(hwnd, False)
            changed += 1
    return changed


def set_border(hwnd: int, on: bool) -> None:
    """Add or remove the thin WS_BORDER frame of a top-level window."""
    if sys.platform != "win32" or not hwnd:
        return
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.windll.user32
    get_style = getattr(user32, "GetWindowLongPtrW", user32.GetWindowLongW)
    set_style = getattr(user32, "SetWindowLongPtrW", user32.SetWindowLongW)
    get_style.restype = ctypes.c_ssize_t
    get_style.argtypes = [wintypes.HWND, ctypes.c_int]
    set_style.restype = ctypes.c_ssize_t
    set_style.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
    style = get_style(hwnd, GWL_STYLE)
    wanted = style | WS_BORDER if on else style & ~WS_BORDER
    if wanted != style:
        set_style(hwnd, GWL_STYLE, wanted)
        user32.SetWindowPos(wintypes.HWND(hwnd), None, 0, 0, 0, 0, SWP_FLAGS)
