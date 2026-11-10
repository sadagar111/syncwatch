"""Recognising YouTube links: everything people copy from the browser, the app or the share button.

    parse("https://youtu.be/dQw4w9WgXcQ?t=42")  ->  VideoRef(id="dQw4w9WgXcQ", start_s=42)

Accepted: youtube.com/watch?v=ID, youtu.be/ID, /shorts/ID, /live/ID, /embed/ID, /v/ID,
youtube-nocookie.com, m. and music. addresses, links without "https://", and a bare 11-character
ID. Timestamps: t=90, t=90s, t=1m30s, t=1h2m3s, start=90, and #t=90.

Only the ID (and where to start) is kept: nothing else from a pasted link ever reaches yt-dlp,
so a link can't smuggle in options or point SyncWatch somewhere else.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional
from urllib.parse import parse_qs, urlsplit

ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
LIST_RE = re.compile(r"^[A-Za-z0-9_-]{10,64}$")       # a playlist ID (PL…, UU…, OLAK5uy_…, RD… mixes)
_HOSTS = ("youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com",
          "youtube-nocookie.com", "www.youtube-nocookie.com", "youtu.be", "www.youtu.be")
_PATH_PREFIXES = ("shorts", "live", "embed", "v", "e")
_TIME_RE = re.compile(r"^(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s?)?$")
MAX_START_S = 48 * 3600


@dataclass(frozen=True)
class VideoRef:
    id: str
    start_s: int = 0

    @property
    def url(self) -> str:
        """The only kind of address SyncWatch hands to yt-dlp."""
        return f"https://www.youtube.com/watch?v={self.id}"

    @property
    def thumbnail(self) -> str:
        return f"https://i.ytimg.com/vi/{self.id}/mqdefault.jpg"


def is_video_id(text: str) -> bool:
    return bool(ID_RE.match(text or ""))


def parse_time(text: str) -> int:
    """'90', '90s', '1m30s', '1h2m3s' -> seconds (0 if it isn't a time)."""
    match = _TIME_RE.match((text or "").strip().lower())
    if not match or not any(match.groups()):
        return 0
    hours, minutes, seconds = (int(part) if part else 0 for part in match.groups())
    return min(MAX_START_S, hours * 3600 + minutes * 60 + seconds)


def parse(text: str) -> Optional[VideoRef]:
    """A pasted link or ID -> VideoRef, or None if it isn't a YouTube video."""
    text = (text or "").strip()
    if not text or len(text) > 2000:
        return None
    if is_video_id(text):
        return VideoRef(text)
    if "://" not in text:
        text = "https://" + text
    try:
        parts = urlsplit(text)
    except ValueError:
        return None
    if parts.scheme not in ("http", "https"):
        return None
    host = (parts.hostname or "").lower()
    if host not in _HOSTS:
        return None
    query = parse_qs(parts.query)
    segments = [segment for segment in parts.path.split("/") if segment]
    video_id = ""
    if host.endswith("youtu.be"):
        video_id = segments[0] if segments else ""
    elif segments[:1] == ["watch"]:
        video_id = (query.get("v") or [""])[0]
    elif len(segments) >= 2 and segments[0] in _PATH_PREFIXES:
        video_id = segments[1]
    if not is_video_id(video_id):
        return None
    start = (query.get("t") or query.get("start") or [""])[0]
    if not start and parts.fragment.startswith("t="):
        start = parts.fragment[2:]
    return VideoRef(video_id, parse_time(start))


def find(text: str) -> Optional[VideoRef]:
    """The first YouTube video in a longer text (a chat message, a whole clipboard)."""
    for word in re.split(r"\s+", text or ""):
        ref = parse(word.strip("<>()[]\"'.,;!?"))
        if ref:
            return ref
    return None


@dataclass(frozen=True)
class PlaylistRef:
    """A playlist link: the playlist, and the video it pointed at inside it (if any)."""
    id: str
    video: Optional[VideoRef] = None

    @property
    def url(self) -> str:
        return f"https://www.youtube.com/playlist?list={self.id}"


def parse_playlist(text: str) -> Optional[PlaylistRef]:
    """youtube.com/playlist?list=…, watch?v=…&list=…, youtu.be/…?list=… -> PlaylistRef, else None."""
    text = (text or "").strip()
    if not text or len(text) > 2000:
        return None
    try:
        parts = urlsplit(text if "://" in text else "https://" + text)
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or (parts.hostname or "").lower() not in _HOSTS:
        return None
    list_id = (parse_qs(parts.query).get("list") or [""])[0]
    if not LIST_RE.match(list_id):
        return None
    return PlaylistRef(list_id, parse(text))


def find_playlist(text: str) -> Optional[PlaylistRef]:
    """The first YouTube playlist link in a longer text."""
    for word in re.split(r"\s+", text or ""):
        ref = parse_playlist(word.strip("<>()[]\"'.,;!?"))
        if ref:
            return ref
    return None
