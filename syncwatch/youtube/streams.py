"""A found YouTube video, as the player needs it (made from resolve.py's JSON).

mpv gets picture and sound as one "EDL" address: ``edl://!new_stream;...%LEN%url;!new_stream;...``.
That's how mpv's own YouTube support joins separate streams: both play as one film, so jumping,
pausing and the position work exactly as with a file. Every address is written with its length in
front (``%LEN%``), so no character in it can break the EDL.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from .links import LIST_RE, is_video_id

EXPIRY_MARGIN_S = 15 * 60        # ask YouTube again when an address has less than this left
CAPTION_LANG_RE = re.compile(r"^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{1,8}){0,3}$")
MAX_CAPTIONS = 300


class BadStream(ValueError):
    pass


@dataclass(frozen=True)
class Caption:
    """Subtitles YouTube has for a video (fetched only when someone picks them)."""
    lang: str          # YouTube's code: "en", "pt-BR"; "en-orig" = the spoken language (automatic)
    name: str          # YouTube's name for it ("English (United Kingdom)"), may be empty
    auto: bool         # made by YouTube (speech recognition, or translated from it), not the uploader

    @property
    def original(self) -> bool:
        return self.auto and self.lang.endswith("-orig")

    @property
    def language(self) -> str:
        return self.lang[:-5] if self.lang.endswith("-orig") else self.lang


def captions_from_json(raw: Any) -> Tuple[Caption, ...]:
    """resolve.py's "captions" list -> Captions; odd entries are left out (another process wrote it)."""
    if not isinstance(raw, list):
        return ()
    found, seen = [], set()
    for item in raw[:MAX_CAPTIONS]:
        if not isinstance(item, dict):
            continue
        lang, name, auto = item.get("lang"), item.get("name"), item.get("auto")
        if not (isinstance(lang, str) and CAPTION_LANG_RE.match(lang) and isinstance(auto, bool)) \
                or (lang, auto) in seen:
            continue
        seen.add((lang, auto))
        name = "".join(ch for ch in name if ch.isprintable())[:80] if isinstance(name, str) else ""
        found.append(Caption(lang, name, auto))
    return tuple(found)


@dataclass
class Stream:
    id: str
    title: str
    channel: str
    duration_s: float
    thumbnail: str
    video_url: str
    audio_url: str = ""
    video_codec: str = ""
    audio_codec: str = ""
    height: int = 0
    fps: float = 0.0
    headers: Dict[str, str] = field(default_factory=dict)
    expires: float = 0.0
    captions: Tuple[Caption, ...] = ()

    kind = "youtube"
    problem = "SW-609"           # reported if the player can't play it

    @property
    def duration_ms(self) -> int:
        return int(self.duration_s * 1000)

    @property
    def label(self) -> str:
        return self.title or f"YouTube video {self.id}"

    def expired(self, now: Optional[float] = None) -> bool:
        now = time.time() if now is None else now
        return now > self.expires - EXPIRY_MARGIN_S

    def address(self) -> str:
        """What mpv opens: the plain address if picture and sound come together, else an EDL."""
        if not self.audio_url:
            return self.video_url
        parts = []
        for url in (self.video_url, self.audio_url):
            parts.append(f"!new_stream;!no_clip;!no_chapters;%{len(url.encode('utf-8'))}%{url}")
        return "edl://" + ";".join(parts)

    def header_fields(self) -> List[str]:
        """For mpv's http-header-fields (the User-Agent goes separately)."""
        return [f"{name}: {value}" for name, value in self.headers.items() if name != "User-Agent"]

    def describe(self) -> str:
        """'1080p · vp9 + opus' for the activity log."""
        codecs = self.video_codec.split(".")[0]
        if self.audio_codec:
            codecs += " + " + self.audio_codec.split(".")[0]
        return f"{self.height}p · {codecs}" if self.height else codecs


def _text(raw: Dict[str, Any], name: str, limit: int = 4000) -> str:
    value = raw.get(name, "")
    if value is None:
        return ""
    if not isinstance(value, str):
        raise BadStream(f"{name}: expected text")
    return value[:limit]


def _url(value: Any) -> str:
    if not isinstance(value, str) or not value.startswith(("https://", "http://")) or len(value) > 20_000:
        raise BadStream("stream address missing or odd")
    if any(ch in value for ch in "\r\n"):
        raise BadStream("stream address with line breaks")
    return value


def from_json(raw: Dict[str, Any]) -> Stream:
    """resolve.py's "ok" object -> Stream (checked: it comes from another process)."""
    if not isinstance(raw, dict):
        raise BadStream("expected an object")
    video_id = _text(raw, "id", 11)
    if not is_video_id(video_id):
        raise BadStream("bad video ID")
    video = raw.get("video")
    if not isinstance(video, dict):
        raise BadStream("no picture stream")
    audio = raw.get("audio") or {}
    if not isinstance(audio, dict):
        raise BadStream("odd sound stream")
    headers = raw.get("headers") or {}
    if not isinstance(headers, dict):
        raise BadStream("odd headers")
    clean_headers = {}
    for name, value in headers.items():   # a header can't add lines or hide a second header
        if (isinstance(name, str) and isinstance(value, str) and 0 < len(name) <= 100 and len(value) <= 1000
                and ":" not in name and not any(ch in name + value for ch in "\r\n\0")):
            clean_headers[name] = value
    try:
        duration = float(raw.get("duration") or 0)
        expires = float(raw.get("expires") or 0)
        height = int(video.get("height") or 0)
        fps = float(video.get("fps") or 0)
    except (TypeError, ValueError) as exc:
        raise BadStream(f"odd number: {exc}") from None
    return Stream(
        id=video_id, title=_text(raw, "title", 200), channel=_text(raw, "channel", 200),
        duration_s=max(0.0, duration), thumbnail=_text(raw, "thumbnail", 2000),
        video_url=_url(video.get("url")), audio_url=_url(audio["url"]) if audio.get("url") else "",
        video_codec=_text(video, "codec", 64), audio_codec=_text(audio, "codec", 64) if audio else "",
        height=max(0, height), fps=max(0.0, fps), headers=clean_headers, expires=expires,
        captions=captions_from_json(raw.get("captions")))


MAX_PLAYLIST = 50


@dataclass(frozen=True)
class PlaylistEntry:
    id: str
    title: str = ""
    duration_s: float = 0.0


@dataclass(frozen=True)
class Playlist:
    id: str
    title: str
    entries: Tuple[PlaylistEntry, ...] = ()


def playlist_from_json(raw: Dict[str, Any]) -> Playlist:
    """resolve.py --playlist's "ok" object -> Playlist (checked)."""
    if not isinstance(raw, dict) or not isinstance(raw.get("playlist"), str) or not LIST_RE.match(raw["playlist"]):
        raise BadStream("bad playlist ID")
    entries, seen = [], set()
    for entry in raw.get("entries") or []:
        if not isinstance(entry, dict) or not is_video_id(entry.get("id") or "") or entry["id"] in seen:
            continue
        seen.add(entry["id"])
        try:
            duration = max(0.0, min(172800.0, float(entry.get("duration") or 0)))
        except (TypeError, ValueError):
            duration = 0.0
        entries.append(PlaylistEntry(entry["id"], _text(entry, "title", 200), duration))
        if len(entries) >= MAX_PLAYLIST:
            break
    return Playlist(raw["playlist"], _text(raw, "title", 200), tuple(entries))
