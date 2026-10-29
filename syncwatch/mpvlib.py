"""Finding libmpv (the library behind SyncWatch's video player) and loading it.

SyncWatch brings its own libmpv, so nobody has to install anything:

* the zip for friends: ``runtime\\<id>\\libmpv\\libmpv-2.dll``, next to SyncWatch's own Python
* the source-code version (a Git folder): ``libmpv\\libmpv-2.dll`` in the project folder,
  fetched once by ``tools\\get_libmpv.py`` (SyncWatch.bat does that by itself)
* Linux and macOS (only for development and the automatic tests): the system's libmpv

Only these places are used. Another mpv that happens to be on the PC (or a DLL planted
in some folder on PATH) is never loaded, so it's always the version that was tested
with this SyncWatch.

``load()`` returns python-mpv's ``mpv`` module, or raises ``Unavailable`` saying why.
The window then falls back to the Qt player (SW-209). Qt-free, like the rest of the
logic, so it can be tested anywhere.
"""
from __future__ import annotations

import ctypes.util
import logging
import os
import sys
from pathlib import Path
from typing import List, Optional

from . import paths

log = logging.getLogger("syncwatch.mpv")

DLL_NAME = "libmpv-2.dll"
DLL_FOLDER = "libmpv"
ENV_DIR = "SYNCWATCH_LIBMPV_DIR"   # tests and tools: use the libmpv in this folder
# The names python-mpv asks ctypes.util.find_library for on Windows.
_PYTHON_MPV_NAMES = ("mpv-2.dll", "libmpv-2.dll", "mpv-1.dll")


class Unavailable(Exception):
    """libmpv or python-mpv can't be loaded. The message says why (it ends up in SW-209's details)."""


_module = None
_failure: Optional[str] = None
_dll_dirs: list = []   # keeps os.add_dll_directory() in effect


def candidate_dirs() -> List[Path]:
    """Where SyncWatch's own libmpv may be, in the order they're tried."""
    dirs = []
    override = os.environ.get(ENV_DIR)
    if override:
        dirs.append(Path(override))
    dirs.append(Path(sys.executable).resolve().parent / DLL_FOLDER)   # installed: runtime\<id>\libmpv
    dirs.append(paths.INSTALL_ROOT / DLL_FOLDER)                      # source-code version
    return dirs


def find_dll() -> Optional[Path]:
    for folder in candidate_dirs():
        dll = folder / DLL_NAME
        if dll.is_file():
            return dll
    return None


def load():
    """python-mpv's module, with SyncWatch's own libmpv loaded. Raises Unavailable."""
    global _module, _failure
    if _module is not None:
        return _module
    if _failure is not None:
        raise Unavailable(_failure)
    try:
        _module = _import()
    except Unavailable as exc:
        _failure = str(exc)
        log.warning("mpv isn't available: %s", exc)
        raise
    log.info("mpv library loaded: %s", getattr(_module.backend, "_name", "?"))
    return _module


def _import():
    if sys.platform != "win32":
        try:
            import mpv
        except ImportError as exc:
            raise Unavailable(f"python-mpv isn't installed ({exc})") from exc
        except OSError as exc:
            raise Unavailable(f"libmpv isn't installed or can't be loaded ({exc})") from exc
        return mpv
    dll = find_dll()
    if dll is None:
        raise Unavailable(f"{DLL_NAME} not found (looked in {', '.join(str(d) for d in candidate_dirs())})")
    try:
        _dll_dirs.append(os.add_dll_directory(str(dll.parent)))
    except (OSError, AttributeError):
        pass
    # python-mpv looks for the DLL with ctypes.util.find_library, which searches every folder on
    # PATH. While it imports, that question gets exactly one answer: our own file.
    original = ctypes.util.find_library

    def ours_only(name):
        return str(dll) if name in _PYTHON_MPV_NAMES else original(name)

    ctypes.util.find_library = ours_only
    try:
        import mpv
    except ImportError as exc:
        raise Unavailable(f"python-mpv isn't installed ({exc})") from exc
    except (OSError, RuntimeError) as exc:   # the DLL can't be loaded, or it's too old
        raise Unavailable(f"{dll} can't be loaded ({exc})") from exc
    finally:
        ctypes.util.find_library = original
    loaded = Path(getattr(mpv.backend, "_name", "") or "")
    if loaded.resolve() != dll.resolve():   # an mpv module imported earlier, from somewhere else
        raise Unavailable(f"a different libmpv was already loaded: {loaded}")
    return mpv


def version_text(player) -> str:
    """'mpv v0.41.0-… (libmpv API 2.6)' for the startup checks and reports."""
    try:
        api = _module.MPV_VERSION if _module is not None else None
        api_text = f" (libmpv API {api[0]}.{api[1]})" if isinstance(api, tuple) else ""
        return f"{player.mpv_version}{api_text}"
    except Exception:  # noqa: BLE001 - only for display
        return "mpv (version unknown)"
