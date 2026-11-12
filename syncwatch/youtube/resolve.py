"""Finding a YouTube video's streams with yt-dlp, in a process of its own (like the converter).

    python -E -P resolve.py <video ID> [--height 1080] [--codecs compatible|any] [--combined]
                            [--js deno=<path>] [--cache <folder>] [--timeout 30]
                            [--client tv,web_safari] [--ip 4|6]
    python -E -P resolve.py <playlist ID> --playlist [--cache <folder>] [--timeout 30]
    python -E -P resolve.py <video ID> --captions <language> [--auto] --out <file.vtt>
                            [--js deno=<path>] [--cache <folder>] [--timeout 30]

Prints exactly one line of JSON (ASCII only) and exits:
    {"ok": {"id", "title", "channel", "duration", "live", "thumbnail", "height",
            "video": {"url", "codec", "height", "fps", "client"},
            "audio": {"url", "codec", "client"} or null,
            "headers": {...}, "expires": <unix time>,
            "captions": [{"lang", "name", "auto"}, ...],
            "warnings": ["<yt-dlp's warnings, without addresses>", ...]}}   exit code 0
    {"ok": {"playlist", "title", "entries": [{"id", "title", "duration"}, ...]}}   (--playlist: at
            most MAX_PLAYLIST videos, only their IDs and titles; nothing is looked up per video)
    {"ok": {"id", "lang", "auto", "file", "bytes"}}   (--captions: the subtitles were written to
            "file": --out, ending in .vtt, or .srt if YouTube had no WebVTT)
    {"error": "<kind>", "message": "<yt-dlp's text>", "warnings": [...]}   exit code 1

Kinds: unavailable, private, signin (age-restricted / members only), bot (YouTube wants proof
this isn't a bot), live, network, outdated (YouTube changed something: yt-dlp needs an update),
missing (yt-dlp isn't installed), nosubs (--captions: no subtitles in that language), other.

--client picks which YouTube apps yt-dlp pretends to be ("tv", "web_safari", "-tv" = the usual
ones without tv; yt-dlp's own default if not given), --ip whether it asks over IPv4 or IPv6.
YouTube ties a stream address to the app and the IP address that asked for it, and sometimes
refuses the streams it gave one app (HTTP 403): "client" says which app each stream came from
(from the address's "c=", e.g. TVHTML5), and the player can ask again another way.

"captions" lists the video's subtitles by language code: the uploader's ("auto": false) and
YouTube's automatic ones ("auto": true; speech recognition, "<code>-orig" for the spoken language,
the rest translated by YouTube). Only codes and names; the text is fetched with --captions when
someone picks one, by yt-dlp itself (YouTube's subtitle addresses need what yt-dlp knows).

Why a separate process: yt-dlp is big, changes every few weeks and runs YouTube's own JavaScript
(through Deno). If it hangs or crashes, SyncWatch just stops the process.

Safety: SyncWatch only ever passes an 11-character video ID (checked again here), and builds the
youtube.com address itself. No cookies, no remote code: yt-dlp may not fetch extra components
(remote_components stays empty), Deno runs without file or network access. Nothing is downloaded
except, with --captions, the one subtitle text asked for (a few hundred KB at most), into a fresh
folder next to --out; only a .vtt or .srt file comes out of it.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence
from urllib.parse import parse_qs, urlsplit

ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
LIST_RE = re.compile(r"^[A-Za-z0-9_-]{10,64}$")          # a playlist ID (PL…, UU…, OLAK5uy_…, RD…)
MAX_PLAYLIST = 50                                        # as many as the queue holds
_IN_URL = re.compile(r"(?:[?&]v=|youtu\.be/|/embed/|/shorts/|/live/)([A-Za-z0-9_-]{11})(?![A-Za-z0-9_-])")
_GONE = ("[private video]", "[deleted video]", "[unavailable video]")
DEFAULT_LIFETIME_S = 5 * 3600     # YouTube's stream addresses usually work about 6 hours
MAX_TITLE = 200
HEADERS_KEPT = ("User-Agent", "Accept", "Accept-Language", "Referer", "Origin")
CLIENT_RE = re.compile(r"^-?[a-z][a-z0-9_]{1,29}$")    # a yt-dlp YouTube client: tv, web_safari, -tv
CLIENT_CODE_RE = re.compile(r"^[A-Z][A-Z0-9_]{1,39}$")  # YouTube's name for it in an address: TVHTML5
MAX_CLIENTS = 6
MAX_WARNINGS = 12
_ADDRESS = re.compile(r"(?i)\b(?:https?|wss?)://[^\s'\"<>]+")
_ANSI = re.compile(r"\x1b\[[0-9;]*m")
CAPTION_LANG_RE = re.compile(r"^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{1,8}){0,3}$")   # en, pt-BR, zh-Hans, en-orig
MAX_CAPTIONS = 300               # YouTube translates its automatic captions into ~150 languages
MAX_CAPTION_BYTES = 3_000_000
CAPTION_SUFFIXES = (".vtt", ".srt")

# What yt-dlp's message says -> kind (first match wins; matched lower-case, so order matters:
# "Requested format is not available" means YouTube changed something, not a removed video)
_KINDS = (
    ("bot", ("confirm you're not a bot", "confirm you’re not a bot", "not a bot", "http error 429",
             "too many requests")),
    ("signin", ("confirm your age", "age-restricted", "age restricted", "inappropriate for some users",
                "members-only", "join this channel", "sign in to")),
    ("private", ("private video",)),
    ("live", ("live event will begin", "premieres in", "this live event")),
    ("outdated", ("requested format is not available", "only images are available", "signature",
                  "nsig", "n challenge", "javascript", "js runtime", "unable to extract", "player response")),
    ("unavailable", ("video unavailable", "is not available", "has been removed", "terminated", "does not exist",
                     "copyright", "no longer available", "http error 404", "blocked it in your country")),
    ("network", ("unable to download webpage", "timed out", "getaddrinfo", "connection", "network is unreachable",
                 "temporary failure in name resolution", "ssl", "urlopen error", "name or service not known")),
    ("outdated", ("please report this issue", "update")),
)


def command(*arguments: str) -> list:
    """How SyncWatch starts this file (same flags as the converter: see library/convert.py)."""
    flags = ["-E"] + (["-P"] if sys.version_info >= (3, 11) else [])
    return [sys.executable, *flags, str(Path(__file__).resolve()), *map(str, arguments)]


def classify(message: str) -> str:
    text = (message or "").lower()
    for kind, needles in _KINDS:
        if any(needle in text for needle in needles):
            return kind
    return "other"


def format_choice(height: int, codecs: str, combined: bool) -> Dict[str, Any]:
    """yt-dlp's format options for what SyncWatch can play.

    mpv plays picture and sound from separate streams (joined with an EDL); Qt's player needs
    one stream with both ("combined"), which YouTube only offers in lower quality.
    "compatible" avoids AV1 (heavy for older PCs without AV1 decoding) unless nothing else exists.
    """
    height = max(144, min(4320, int(height)))
    sort = [f"res:{height}", "fps", "hdr:sdr"]
    if codecs == "compatible":
        sort.append("vcodec:vp9")
    if combined:
        return {"format": "b[vcodec!=none][acodec!=none]/b", "format_sort": [*sort, "acodec:aac"]}
    return {"format": "bv*+ba/b", "format_sort": sort}


def _expires(urls: List[str], now: float) -> float:
    times = []
    for url in urls:
        try:
            value = parse_qs(urlsplit(url).query).get("expire", [""])[0]
        except ValueError:
            value = ""
        if value.isdigit():
            times.append(float(value))
    return min(times) if times else now + DEFAULT_LIFETIME_S


def _client(url: str) -> str:
    """Which YouTube app a stream address was given to ("c=TVHTML5"), or ""."""
    try:
        value = parse_qs(urlsplit(url).query).get("c", [""])[0]
    except ValueError:
        return ""
    return value if CLIENT_CODE_RE.match(value) else ""


def clean_warning(message: Any) -> str:
    """One of yt-dlp's warnings, fit for SyncWatch's log: no addresses (they hold the IP address
    and keys), no colours or control characters, short."""
    text = _ANSI.sub("", str(message or "")).strip()
    if text[:8].upper() == "WARNING:":
        text = text[8:]
    text = _ADDRESS.sub("<address>", text)
    text = "".join(ch if ch.isprintable() else " " for ch in text)
    return " ".join(text.split())[:300]


class WarningCollector:
    """yt-dlp's "logger": keeps its warnings (cleaned, at most MAX_WARNINGS) and drops the rest.
    It also keeps yt-dlp from printing anything: this process's output is one line of JSON."""

    def __init__(self, into: List[str]):
        self.into = into

    def debug(self, message) -> None:
        pass

    info = debug

    def warning(self, message) -> None:
        text = clean_warning(message)
        if text and text not in self.into and len(self.into) < MAX_WARNINGS:
            self.into.append(text)

    def error(self, message) -> None:
        pass


def _headers(fmt: Dict[str, Any]) -> Dict[str, str]:
    raw = fmt.get("http_headers") or {}
    return {key: str(value) for key, value in raw.items() if key in HEADERS_KEPT and value}


def summarize(info: Dict[str, Any], now: Optional[float] = None) -> Dict[str, Any]:
    """yt-dlp's (processed) info -> the small JSON SyncWatch needs. Raises ValueError if unusable."""
    now = time.time() if now is None else now
    if info.get("is_live") or info.get("live_status") in ("is_live", "is_upcoming", "post_live"):
        raise LookupError("live")
    picked = info.get("requested_formats") or [info]
    # vcodec "none" = sound only; missing = unknown (a plain file: assume it has both)
    video = next((f for f in picked if f.get("vcodec") != "none"), None)
    audio = next((f for f in picked if f is not video and f.get("vcodec") == "none"
                  and f.get("acodec") not in (None, "none")), None)
    if video is None or not video.get("url"):
        raise ValueError("no playable picture stream")
    urls = [video["url"]] + ([audio["url"]] if audio else [])
    return {
        "id": info.get("id", ""),
        "title": str(info.get("title") or info.get("id") or "")[:MAX_TITLE],
        "channel": str(info.get("channel") or info.get("uploader") or "")[:MAX_TITLE],
        "duration": float(info.get("duration") or 0),
        "live": False,
        "thumbnail": str(info.get("thumbnail") or ""),
        "height": int(video.get("height") or 0),
        "video": {"url": video["url"], "codec": str(video.get("vcodec") or ""), "height": int(video.get("height") or 0),
                  "fps": float(video.get("fps") or 0), "client": _client(video["url"])},
        "audio": ({"url": audio["url"], "codec": str(audio.get("acodec") or ""), "client": _client(audio["url"])}
                  if audio else None),
        "headers": _headers(video),
        "expires": _expires(urls, now),
        "captions": caption_list(info),
    }


def caption_list(info: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The video's subtitles: {"lang", "name", "auto"} per language, the uploader's first.
    Non-languages (YouTube's "live_chat" replay) are left out by the pattern."""
    found: List[Dict[str, Any]] = []
    for auto, key in ((False, "subtitles"), (True, "automatic_captions")):
        table = info.get(key)
        if not isinstance(table, dict):
            continue
        for lang, formats in table.items():
            if not (isinstance(lang, str) and CAPTION_LANG_RE.match(lang) and isinstance(formats, list) and formats):
                continue
            name = next((f["name"] for f in formats if isinstance(f, dict) and isinstance(f.get("name"), str)), "")
            found.append({"lang": lang, "name": "".join(ch for ch in name if ch.isprintable())[:80], "auto": auto})
            if len(found) >= MAX_CAPTIONS:
                return found
    return found


def ydl_options(height: int, codecs: str, combined: bool, js: Optional[str], cache: Optional[str],
                timeout: float, clients: Sequence[str] = (), ip: int = 0,
                warnings: Optional[List[str]] = None) -> Dict[str, Any]:
    """yt-dlp's options. ``clients``: which YouTube apps to ask as (see the module text), checked by
    the caller with CLIENT_RE; ``ip``: 4 or 6 to ask only over that, 0 = as Windows chooses;
    ``warnings``: a list that receives yt-dlp's warnings."""
    options: Dict[str, Any] = {
        "quiet": True, "no_warnings": True, "noprogress": True, "noplaylist": True,
        "skip_download": True, "socket_timeout": timeout, "color": {"stdout": "never", "stderr": "never"},
        "remote_components": [],            # never fetch extra code
        "cachedir": cache or False,         # remembers YouTube's player code: much faster next time
        **format_choice(height, codecs, combined),
    }
    if js:
        name, _, path = js.partition("=")
        options["js_runtimes"] = {name.strip().lower(): ({"path": path} if path else {})}
    if clients:
        options["extractor_args"] = {"youtube": {"player_client": list(clients)}}
    if ip in (4, 6):
        options["source_address"] = "0.0.0.0" if ip == 4 else "::"     # what --force-ipv4/6 do
    if warnings is not None:
        options["logger"] = WarningCollector(warnings)
    return options


def parse_clients(text: str) -> List[str]:
    """"tv,web_safari" -> ["tv", "web_safari"]; ValueError for anything that isn't a client name."""
    names = [name.strip() for name in (text or "").split(",") if name.strip()]
    if len(names) > MAX_CLIENTS or not all(CLIENT_RE.match(name) for name in names):
        raise ValueError(f"not a list of YouTube client names: {text[:100]!r}")
    return names


def resolve(target: str, options: Dict[str, Any]) -> Dict[str, Any]:
    import yt_dlp  # noqa: PLC0415 - only in this process
    with yt_dlp.YoutubeDL(options) as ydl:
        info = ydl.extract_info(target, download=False)
        return summarize(ydl.sanitize_info(info))


def caption_options(lang: str, auto: bool, folder: Path, js: Optional[str], cache: Optional[str],
                    timeout: float) -> Dict[str, Any]:
    """yt-dlp's options for writing one language's subtitles into ``folder`` and nothing else."""
    options: Dict[str, Any] = {
        "quiet": True, "no_warnings": True, "noprogress": True, "noplaylist": True,
        "skip_download": True, "socket_timeout": timeout, "color": {"stdout": "never", "stderr": "never"},
        "remote_components": [], "cachedir": cache or False,
        "ignore_no_formats_error": True,                   # only the subtitles matter here
        "writesubtitles": not auto, "writeautomaticsub": auto,
        "subtitleslangs": [re.escape(lang)],               # (yt-dlp reads these as patterns)
        "subtitlesformat": "vtt/srt",
        "outtmpl": {"default": str(folder / "caption.%(ext)s")},
    }
    if js:
        name, _, path = js.partition("=")
        options["js_runtimes"] = {name.strip().lower(): ({"path": path} if path else {})}
    return options


def fetch_captions(target: str, options: Dict[str, Any], folder: Path, out: Path) -> Dict[str, Any]:
    """Let yt-dlp write the subtitles into ``folder``, then move them to ``out``. LookupError if the
    video has none in that language; ValueError if what came is too big."""
    import yt_dlp  # noqa: PLC0415 - only in this process
    with yt_dlp.YoutubeDL(options) as ydl:
        ydl.extract_info(target, download=True)        # skip_download: only the subtitles are written
    written = sorted(p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in CAPTION_SUFFIXES)
    if not written:
        raise LookupError("no subtitles in that language")
    source = written[0]
    size = source.stat().st_size
    if not 0 < size <= MAX_CAPTION_BYTES:
        raise ValueError(f"the subtitles are {size} bytes")
    final = out.with_suffix(source.suffix.lower())
    os.replace(source, final)
    return {"file": str(final), "bytes": size}


def summarize_playlist(info: Dict[str, Any], limit: int = MAX_PLAYLIST) -> Dict[str, Any]:
    """yt-dlp's flat playlist (IDs and titles only) -> our answer; private/deleted videos left out."""
    entries, seen = [], set()
    for entry in info.get("entries") or []:
        if not isinstance(entry, dict):
            continue
        video = entry.get("id") if isinstance(entry.get("id"), str) and ID_RE.match(entry["id"]) else ""
        if not video and isinstance(entry.get("url"), str):
            match = _IN_URL.search(entry["url"])
            video = match.group(1) if match else ""
        title = entry.get("title") if isinstance(entry.get("title"), str) else ""
        if not video or video in seen or title.strip().lower() in _GONE:
            continue
        seen.add(video)
        duration = entry.get("duration")
        duration = float(duration) if isinstance(duration, (int, float)) and not isinstance(duration, bool) else 0.0
        entries.append({"id": video, "title": title[:MAX_TITLE], "duration": max(0.0, min(duration, 172800.0))})
        if len(entries) >= limit:
            break
    title = info.get("title") if isinstance(info.get("title"), str) else ""
    return {"title": title[:MAX_TITLE], "entries": entries}


def playlist_options(cache: Optional[str], timeout: float) -> Dict[str, Any]:
    return {"quiet": True, "no_warnings": True, "noprogress": True, "skip_download": True,
            "socket_timeout": timeout, "color": {"stdout": "never", "stderr": "never"},
            "remote_components": [], "cachedir": cache or False,
            "extract_flat": "in_playlist", "playlistend": MAX_PLAYLIST, "lazy_playlist": True}


def list_playlist(target: str, options: Dict[str, Any]) -> Dict[str, Any]:
    import yt_dlp  # noqa: PLC0415 - only in this process
    with yt_dlp.YoutubeDL(options) as ydl:
        info = ydl.extract_info(target, download=False)
        return summarize_playlist(ydl.sanitize_info(info))


def say(message: Dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(message, ensure_ascii=True) + "\n")
    sys.stdout.flush()


def main(argv: List[str]) -> int:
    import argparse  # noqa: PLC0415
    parser = argparse.ArgumentParser(prog="resolve.py", description=__doc__.split("\n\n")[0])
    parser.add_argument("video", help="11-character YouTube video ID (with --playlist: a playlist ID)")
    parser.add_argument("--playlist", action="store_true", help="list a playlist's videos instead")
    parser.add_argument("--height", type=int, default=1080)
    parser.add_argument("--codecs", choices=("compatible", "any"), default="compatible")
    parser.add_argument("--combined", action="store_true", help="one stream with picture and sound (Qt's player)")
    parser.add_argument("--js", default="", help="JavaScript runtime, e.g. deno=C:\\...\\deno.exe")
    parser.add_argument("--cache", default="")
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--client", default="", help="YouTube apps to ask as, e.g. tv,web_safari (yt-dlp's choice if empty)")
    parser.add_argument("--ip", type=int, choices=(0, 4, 6), default=0, help="ask only over IPv4 / IPv6")
    parser.add_argument("--captions", default="", help="fetch the subtitles in this language instead")
    parser.add_argument("--auto", action="store_true", help="with --captions: YouTube's automatic ones")
    parser.add_argument("--out", default="", help="with --captions: where they go (a .vtt file)")
    parser.add_argument("--test-url", default="", help=argparse.SUPPRESS)   # tests only: a local address
    args = parser.parse_args(argv)
    if args.playlist:
        return _main_playlist(args)
    if not ID_RE.match(args.video):
        say({"error": "other", "message": "not a YouTube video ID"})
        return 2
    target = args.test_url or f"https://www.youtube.com/watch?v={args.video}"
    if args.captions or args.out:
        return _main_captions(args, target)
    try:
        import yt_dlp  # noqa: F401, PLC0415
    except ImportError as exc:
        say({"error": "missing", "message": f"yt-dlp isn't installed ({exc})"})
        return 1
    try:
        clients = parse_clients(args.client)
    except ValueError as exc:
        say({"error": "other", "message": str(exc)})
        return 2
    warnings: List[str] = []
    options = ydl_options(args.height, args.codecs, args.combined, args.js or None, args.cache or None, args.timeout,
                          clients, args.ip, warnings)
    try:
        result = resolve(target, options)
    except LookupError:
        say({"error": "live", "message": "live streams aren't supported yet", "warnings": warnings})
        return 1
    except Exception as exc:  # noqa: BLE001 - everything yt-dlp can throw ends up here
        text = f"{type(exc).__name__}: {exc}"
        say({"error": classify(text), "message": text[:2000], "warnings": warnings})
        return 1
    result["id"] = args.video
    result["warnings"] = warnings
    say({"ok": result})
    return 0


def _main_captions(args, target: str) -> int:
    if not CAPTION_LANG_RE.match(args.captions) or not args.out.lower().endswith(".vtt"):
        say({"error": "other", "message": "--captions needs a language code and --out a .vtt file"})
        return 2
    try:
        import yt_dlp  # noqa: F401, PLC0415
    except ImportError as exc:
        say({"error": "missing", "message": f"yt-dlp isn't installed ({exc})"})
        return 1
    out = Path(args.out)
    folder = out.parent / f".captions-{os.getpid()}"
    options = caption_options(args.captions, args.auto, folder, args.js or None, args.cache or None, args.timeout)
    try:
        folder.mkdir(parents=True, exist_ok=True)
        result = fetch_captions(target, options, folder, out)
    except LookupError as exc:
        say({"error": "nosubs", "message": f"{exc} ({args.captions}{', automatic' if args.auto else ''})"})
        return 1
    except Exception as exc:  # noqa: BLE001 - everything yt-dlp can throw ends up here
        text = f"{type(exc).__name__}: {exc}"
        say({"error": classify(text), "message": text[:2000]})
        return 1
    finally:
        shutil.rmtree(folder, ignore_errors=True)
    result.update(id=args.video, lang=args.captions, auto=args.auto)
    say({"ok": result})
    return 0


def _main_playlist(args) -> int:
    if not LIST_RE.match(args.video):
        say({"error": "other", "message": "not a YouTube playlist ID"})
        return 2
    target = args.test_url or f"https://www.youtube.com/playlist?list={args.video}"
    try:
        import yt_dlp  # noqa: F401, PLC0415
    except ImportError as exc:
        say({"error": "missing", "message": f"yt-dlp isn't installed ({exc})"})
        return 1
    try:
        result = list_playlist(target, playlist_options(args.cache or None, args.timeout))
    except Exception as exc:  # noqa: BLE001 - everything yt-dlp can throw ends up here
        text = f"{type(exc).__name__}: {exc}"
        say({"error": classify(text), "message": text[:2000]})
        return 1
    result["playlist"] = args.video
    say({"ok": result})
    return 0


if __name__ == "__main__":
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    sys.exit(main(sys.argv[1:]))
