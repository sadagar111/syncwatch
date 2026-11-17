"""The parts YouTube needs, as found on this PC: yt-dlp (finds the streams) and Deno (runs
YouTube's JavaScript for yt-dlp). Used by the window, the startup checks and tools/youtube_check.py."""
from __future__ import annotations

import importlib.metadata
import importlib.util
from pathlib import Path
from typing import Dict, Optional


def find_js_runtime(setting: str) -> Optional[str]:
    """Deno for yt-dlp: the one chosen in the settings, else the one inside SyncWatch's runtime."""
    if setting:
        return setting if Path(setting).is_file() else None
    try:
        from deno import find_deno_bin  # noqa: PLC0415 - the "deno" package only holds deno.exe
        return find_deno_bin()
    except (ImportError, FileNotFoundError):
        return None


def ytdlp_installed() -> bool:
    return importlib.util.find_spec("yt_dlp") is not None


def versions() -> Dict[str, str]:
    """{"yt-dlp": "2026.10.26", "yt-dlp-ejs": "0.8.0", "deno": "2.9.8"}: "" for what's missing."""
    found = {}
    for name in ("yt-dlp", "yt-dlp-ejs", "deno"):
        try:
            found[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            found[name] = ""
    return found
