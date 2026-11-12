"""Which ways of asking YouTube work on this PC? For SW-609 ("Couldn't play the YouTube video").

    tools\\youtube_check.bat                   with a short test video
    tools\\youtube_check.bat <YouTube link>    with that video

SyncWatch finds a video's streams with yt-dlp (syncwatch/youtube/resolve.py). yt-dlp asks
YouTube as one of YouTube's own apps ("clients": the TV app, Safari, the embedded player...),
over IPv4 or IPv6. YouTube ties every stream address to the app and the IP address that asked,
and sometimes refuses the streams it gave out (HTTP 403). For each way of asking, this:

  1. asks yt-dlp exactly as SyncWatch does (its Deno, its cache, your quality setting);
  2. fetches the first 64 KB of the picture and of the sound, with yt-dlp's headers, over the
     same IP version yt-dlp used;
  3. lets SyncWatch's own mpv open them the way the window does. (Only where Windows chooses
     the IP version: mpv can't be told to use IPv4 or IPv6.)

It prints a table and saves it as youtube_check.txt in SyncWatch's log folder. Nothing else is
saved or changed, and no stream address or IP address is printed or saved. About a minute.
"""
from __future__ import annotations

import http.client
import json
import re
import socket
import ssl
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from syncwatch import __version__, errors, mpvlib, paths  # noqa: E402
from syncwatch.settings import Settings  # noqa: E402
from syncwatch.youtube import links, parts, resolve, streams  # noqa: E402

TEST_VIDEO = "jNQXAC9IVRw"          # "Me at the zoo": 19 seconds, the first video on YouTube
FETCH_BYTES = 65536
FETCH_TIMEOUT_S = 12
RESOLVE_TIMEOUT_S = 90
MPV_TIMEOUT_S = 20

# (what the table calls it, --client, --ip). The first one is what SyncWatch 0.5.1 does.
WAYS: Tuple[Tuple[str, str, int], ...] = (
    ("as SyncWatch 0.5.1 does", "", 0),
    ("the same, IPv4 only", "", 4),
    ("the same, IPv6 only", "", 6),
    ("TV app (tv)", "tv", 0),
    ("Safari (web_safari)", "web_safari", 0),
    ("embedded player (web_embedded)", "web_embedded", 0),
    ("mobile site (mweb)", "mweb", 0),
    ("Android VR (android_vr)", "android_vr", 0),
    ("website (web)", "web", 0),
)


# ------------------------------------------------------------------ the connection itself
def internet(family: int) -> bool:
    """Can this PC reach YouTube over IPv4 (socket.AF_INET) / IPv6 (AF_INET6)?"""
    try:
        for *_, address in socket.getaddrinfo("www.youtube.com", 443, family, socket.SOCK_STREAM)[:2]:
            with socket.socket(family, socket.SOCK_STREAM) as sock:
                sock.settimeout(4)
                sock.connect(address)
                return True
    except OSError:
        pass
    return False


def fetch_start(url: str, headers: Dict[str, str], ip: int) -> str:
    """Ask for the first bytes of a stream, as a player would: "ok", "refused (403)", "HTTP <n>"
    or "no connection (...)". ``ip`` 4 / 6 forces that IP version (0: as Windows chooses)."""
    try:
        parts_ = urlsplit(url)
        host, port = parts_.hostname or "", parts_.port or (443 if parts_.scheme == "https" else 80)
    except ValueError:
        return "odd address"
    source = ("0.0.0.0", 0) if ip == 4 else ("::", 0) if ip == 6 else None
    if parts_.scheme == "https":
        conn = http.client.HTTPSConnection(host, port, timeout=FETCH_TIMEOUT_S, source_address=source,
                                           context=ssl.create_default_context())
    else:
        conn = http.client.HTTPConnection(host, port, timeout=FETCH_TIMEOUT_S, source_address=source)
    path = (parts_.path or "/") + (f"?{parts_.query}" if parts_.query else "")
    try:
        conn.request("GET", path, headers={**headers, "Range": f"bytes=0-{FETCH_BYTES - 1}"})
        response = conn.getresponse()
        data = response.read(FETCH_BYTES)
        status = response.status
    except (OSError, http.client.HTTPException) as exc:
        return f"no connection ({type(exc).__name__})"
    finally:
        conn.close()
    if status in (200, 206) and data:
        return "ok"
    return "refused (403)" if status == 403 else f"HTTP {status}"


# ------------------------------------------------------------------ yt-dlp, as SyncWatch runs it
def find_streams(video: str, settings: Settings, profile: paths.Profile, client: str, ip: int) -> Dict:
    """resolve.py's answer ({"ok": ...} or {"error": ...}), started like the window starts it."""
    arguments = ["--height", str(settings.get("youtube.quality")), "--codecs", str(settings.get("youtube.codecs")),
                 "--cache", str(profile.cache_dir / "yt-dlp"), "--timeout", "30"]
    deno = parts.find_js_runtime(settings.get("youtube.js_runtime"))
    if deno:
        arguments += ["--js", f"deno={deno}"]
    if client:
        arguments += ["--client", client]
    if ip:
        arguments += ["--ip", str(ip)]
    try:
        done = subprocess.run(resolve.command(video, *arguments), capture_output=True, text=True,
                              timeout=RESOLVE_TIMEOUT_S, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except subprocess.TimeoutExpired:
        return {"error": "timeout", "message": f"no answer in {RESOLVE_TIMEOUT_S} s", "warnings": []}
    lines = [line for line in done.stdout.splitlines() if line.strip()]
    try:
        return json.loads(lines[-1])
    except (IndexError, ValueError):
        return {"error": "other", "message": resolve.clean_warning(done.stderr[-300:]), "warnings": []}


# ------------------------------------------------------------------ mpv, as the window runs it
class MpvTry:
    """SyncWatch's own libmpv without a picture or sound device: opens a stream like the window
    (same headers) and says whether it starts playing."""

    def __init__(self):
        self.module = mpvlib.load()
        options = {"idle": "yes", "vo": "null", "ao": "null", "hwdec": "no", "mute": "yes", "pause": "no",
                   "osc": "no", "load_scripts": "no", "ytdl": "no", "load_stats_overlay": "no", "config": "no",
                   "terminal": "no", "input_default_bindings": "no"}
        self.player, failure = None, None
        for lua_off in mpvlib.lua_scripts_off(self.module):
            try:
                self.player = self.module.MPV(log_handler=self._log, loglevel="warn", **{**lua_off, **options})
                break
            except Exception as exc:  # noqa: BLE001 - try the next set, as the window does
                failure = exc
        if self.player is None:
            raise mpvlib.Unavailable(errors.describe_exception(failure))
        self.lines: List[str] = []
        self.result: Optional[str] = None
        self.done = threading.Event()       # this try has an answer
        self.ended = threading.Event()      # ... and mpv has closed the stream again
        self.player.register_event_callback(self._event)
        self.version = mpvlib.version_text(self.player)

    def _log(self, level, prefix, text) -> None:
        self.lines.append(f"[{prefix}] {resolve.clean_warning(text)}")

    def _event(self, event) -> None:
        kind = event.event_id.value
        if kind == self.module.MpvEventID.PLAYBACK_RESTART and self.result is None:
            self.result = "plays"
            self.done.set()
        elif kind == self.module.MpvEventID.END_FILE:
            if self.result is None:
                self.result = self._why(getattr(event.data, "error", 0))
                self.done.set()
            self.ended.set()

    def _why(self, error) -> str:
        said = " ".join(self.lines).lower()
        if "403" in said:
            return "refused (403)"
        for code in ("404", "410", "429"):
            if f"http error {code}" in said:
                return f"HTTP {code}"
        if isinstance(error, int) and error < 0:
            try:
                return "failed: " + self.module._mpv_error_string(error).decode("utf-8", "replace")
            except Exception:  # noqa: BLE001
                return f"failed (error {error})"
        return "stopped without playing"

    def play(self, raw: Dict) -> str:
        stream = streams.from_json(raw)
        self.lines.clear()
        self.result = None
        self.done.clear()
        self.ended.clear()
        # the same headers as the window (mpv_player._set_headers): each field as one item
        self.player["user-agent"] = stream.headers.get("User-Agent", "") or "libmpv"
        self.player.command("change-list", "http-header-fields", "clr", "")
        for field in stream.header_fields():
            self.player.command("change-list", "http-header-fields", "append", field)
        self.player.loadfile(stream.address())
        if not self.done.wait(MPV_TIMEOUT_S):
            self.result = f"no answer in {MPV_TIMEOUT_S} s"
        try:
            self.player.command("stop")
        except Exception:  # noqa: BLE001
            pass
        self.ended.wait(5)                 # (so this stream's end isn't taken for the next one's)
        return self.result or "?"

    def close(self) -> None:
        try:
            self.player.terminate()
        except Exception:  # noqa: BLE001
            pass


# ------------------------------------------------------------------ the check
def describe(found: Dict) -> str:
    video, audio = found.get("video") or {}, found.get("audio") or {}
    codecs = str(video.get("codec", "")).split(".")[0] + ("+" + str(audio.get("codec", "")).split(".")[0] if audio else "")
    apps = video.get("client") or "?"
    if audio and audio.get("client") and audio.get("client") != video.get("client"):
        apps += "/" + audio["client"]
    return f"{found.get('height', 0)}p {codecs} · {apps}"


def check(video: str, show: bool = True) -> List[str]:
    """Tries every way; returns the report's lines (and prints them, with progress, if ``show``)."""
    report: List[str] = []

    def say(text: str = "") -> None:
        report.append(text)
        if show:
            print(text)

    def progress(text: str, end: str = "\n") -> None:
        if show:
            print(text, end=end, flush=True)

    profile = paths.Profile("default")
    settings = Settings(profile.settings_file)
    settings.load()
    found_parts = parts.versions()
    deno = parts.find_js_runtime(settings.get("youtube.js_runtime"))
    v4, v6 = internet(socket.AF_INET), internet(socket.AF_INET6)
    say(f"SyncWatch YouTube check · SyncWatch {__version__} · yt-dlp {found_parts['yt-dlp'] or 'missing'} · "
        f"yt-dlp-ejs {found_parts['yt-dlp-ejs'] or 'missing'} · Deno {'found' if deno else 'missing'}")
    say(f"{errors.system_summary()}")
    say(f"Video {video} · quality setting {settings.get('youtube.quality')}p · "
        f"internet: IPv4 {'yes' if v4 else 'no'}, IPv6 {'yes' if v6 else 'no'}")
    try:
        mpv: Optional[MpvTry] = MpvTry()
        say(f"mpv: {mpv.version}")
    except Exception as exc:  # noqa: BLE001 - the rest still says a lot
        mpv = None
        say(f"mpv can't start here ({resolve.clean_warning(exc)}): the mpv column stays empty")
    ways = [way for way in WAYS if not (way[2] == 6 and not v6) and not (way[2] == 4 and not v4)]
    progress(f"\nTrying {len(ways)} ways, about {len(ways) * 7} seconds...")
    rows, warnings, started = [], [], time.monotonic()
    for number, (name, client, ip) in enumerate(ways, 1):
        progress(f"  [{number}/{len(ways)}] {name}...", end="")
        began = time.monotonic()
        answer = find_streams(video, settings, profile, client, ip)
        for warning in (answer.get("warnings") or []) + ((answer.get("ok") or {}).get("warnings") or []):
            warning = short(warning)
            if warning and warning not in warnings:
                warnings.append(warning)
        if "ok" in answer:
            found = answer["ok"]
            headers = found.get("headers") or {}
            picture = fetch_start(found["video"]["url"], headers, ip)
            sound = fetch_start(found["audio"]["url"], headers, ip) if found.get("audio") else "(in the picture)"
            played = mpv.play(found) if (mpv is not None and ip == 0) else "-"
            rows.append((name, describe(found), picture, sound, played))
        else:
            kind = answer.get("error", "other")
            rows.append((name, f"not found: {kind}", "-", "-", "-"))
            message = short(answer.get("message", ""))
            if message and message not in warnings:
                warnings.append(message)
        took = time.monotonic() - began
        left = (time.monotonic() - started) / number * (len(ways) - number)
        progress(f" {took:.0f} s" + (f" · about {left:.0f} s left" if number < len(ways) else ""))
    if mpv is not None:
        mpv.close()

    say()
    widths = [max(len(row[i]) for row in rows + [("Way of asking", "Found", "Picture", "Sound", "mpv")])
              for i in range(5)]
    for row in [("Way of asking", "Found", "Picture", "Sound", "mpv")] + rows:
        say("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)).rstrip())
    say()
    for line in verdict(rows, v4, v6):
        say(line)
    if warnings:
        say()
        say("yt-dlp said:")
        for warning in warnings[:20]:
            say(f"  {warning}")
    return report


def short(message: str) -> str:
    """yt-dlp's message, without addresses, its "ERROR: [youtube] <id>:" and its retry counts."""
    text = resolve.clean_warning(message)
    text = re.sub(r"^(?:\w+Error: )?(?:ERROR: )?(?:\[youtube\] (?:[A-Za-z0-9_-]{11}: )?)?", "", text)
    text = re.sub(r"\s*Retrying \(\d+/\d+\)\.\.\.$", "", text)
    text = re.sub(r";?\s*please report this issue.*$", "", text)
    return text[:200]


def verdict(rows: List[Tuple[str, ...]], v4: bool, v6: bool) -> List[str]:
    by_name = {row[0]: row for row in rows}
    first = rows[0] if rows else None
    lines = []
    kinds = {row[1] for row in rows}
    if kinds == {"not found: network"}:
        lines.append("Result: yt-dlp couldn't reach YouTube at all. Check the internet connection, a VPN, a proxy "
                     "or a firewall.")
    elif kinds == {"not found: bot"}:
        lines.append("Result: YouTube wants proof that this isn't a bot (it asks that of some networks and VPNs).")
    elif first and first[4] == "plays":
        lines.append("Result: YouTube works on this PC right now, the way SyncWatch 0.5.1 asks.")
    else:
        playing = [row[0] for row in rows if row[4] == "plays"]
        fetched = [row[0] for row in rows if row[2] == "ok" and row[3] in ("ok", "(in the picture)")]
        if playing:
            lines.append("Result: the way 0.5.1 asks doesn't work here, but these do: " + ", ".join(playing) + ".")
        elif fetched:
            lines.append("Result: mpv played none of them, but YouTube sent the streams to Python for: "
                         + ", ".join(fetched) + ".")
        else:
            lines.append("Result: none of the ways worked: YouTube refuses this PC's requests (or yt-dlp needs "
                         "an update).")
    four, six = by_name.get("the same, IPv4 only"), by_name.get("the same, IPv6 only")
    if four and six and (four[2] == "ok") != (six[2] == "ok"):
        good, other = (four, six) if four[2] == "ok" else (six, four)
        good_name, bad_name = ("IPv4", "IPv6") if good is four else ("IPv6", "IPv4")
        how = "it refuses them" if other[2] == "refused (403)" else f"it didn't work ({other[2]})"
        lines.append(f"Over {good_name} YouTube sends the streams, over {bad_name} {how}.")
    if not v6:
        lines.append("(This PC has no IPv6 connection to YouTube, so that way wasn't tried.)")
    lines.append("Send this text to Claude, or the file below. It holds no stream or IP addresses.")
    return lines


def main(argv: List[str]) -> int:
    try:
        sys.stdout.reconfigure(errors="replace")
    except (AttributeError, ValueError):
        pass
    video = TEST_VIDEO
    if argv:
        found = links.parse(" ".join(argv))
        if found is None:
            print("That isn't a YouTube link. Use: tools\\youtube_check.bat [YouTube link]")
            return 2
        video = found.id
    report = check(video)
    profile = paths.Profile("default")
    try:
        profile.logs_dir.mkdir(parents=True, exist_ok=True)
        target = profile.logs_dir / "youtube_check.txt"
        target.write_text("\n".join(report) + "\n", encoding="utf-8")
        print(f"\nSaved: {target}")
    except OSError as exc:
        print(f"\n(Couldn't save the result: {exc})")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
