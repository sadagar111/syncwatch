"""Magnet links and .torrent files: what counts as one (no libtorrent needed for this)."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, unquote, urlsplit

_BTIH = re.compile(r"^urn:btih:([0-9a-fA-F]{40}|[A-Za-z2-7]{32})$")
_BTMH = re.compile(r"^urn:btmh:1220[0-9a-fA-F]{64}$")
MAX_TORRENT_FILE = 20 * 1024 * 1024      # .torrent files are small; anything bigger isn't one


def is_magnet(text: str) -> bool:
    return magnet_hash(text) is not None


def magnet_hash(text: str) -> Optional[str]:
    """The info-hash of a magnet link (lower-case hex or base32 as written), or None."""
    text = (text or "").strip()
    if not text.lower().startswith("magnet:?") or len(text) > 8000 or any(ch in text for ch in "\r\n\0"):
        return None
    try:
        query = parse_qs(urlsplit(text).query)
    except ValueError:
        return None
    for value in query.get("xt", []):
        match = _BTIH.match(value)
        if match:
            return match.group(1).lower()
        if _BTMH.match(value):
            return value[9:].lower()
    return None


def magnet_name(text: str) -> str:
    try:
        return unquote((parse_qs(urlsplit(text.strip()).query).get("dn") or [""])[0])[:200]
    except ValueError:
        return ""


def find_magnet(text: str) -> Optional[str]:
    """The first magnet link in a longer text (a clipboard, a chat message)."""
    for word in re.split(r"\s+", text or ""):
        word = word.strip("<>()[]\"'")
        if is_magnet(word):
            return word
    return None


def is_torrent_file(path: Path) -> bool:
    try:
        return path.suffix.lower() == ".torrent" and 0 < path.stat().st_size <= MAX_TORRENT_FILE
    except OSError:
        return False
