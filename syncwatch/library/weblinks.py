"""Films at web addresses: a link to a film file (https://.../Film.mkv, e.g. from Real-Debrid) or to a
.torrent file (since 0.5.1). No Qt in here; ui/web_ui.py runs these in the background.

    parse(text)          -> WebLink, or None if it isn't an http(s) address (YouTube links are found first)
    room_ok(url)         -> may a link that came from the room be opened? (https, a public host, no password)
    probe(url)           -> Info(name, size, kind, ranges), asked from the server (HEAD, else the first byte)
    fetch_torrent(url)   -> a .torrent file's bytes (checked; at most 20 MB)
    Download(...).run()  -> saves the film into a folder; a stopped download continues where it was

A link can carry an access token (a Real-Debrid link is one), so links are never logged: logs name
only the host.
"""
from __future__ import annotations

import http.client
import ipaddress
import json
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import unquote, urlsplit

from .films import VIDEO_EXTENSIONS, space_problem, unique_path

MAX_LINK = 2000                         # characters; also what the room accepts
MAX_TORRENT_FILE = 20 * 1024 * 1024     # (torrent/links.py has the same limit)
TIMEOUT_S = 20                          # no answer for this long: the connection is gone
CHUNK = 1 << 20
PART = ".sw-download"                   # "<film><PART>" while it downloads; its notes in "<film><PART>.json"
USER_AGENT = "SyncWatch"
LOCAL_SUFFIXES = (".localhost", ".local", ".lan", ".home", ".internal", ".intranet", ".corp", ".home.arpa")
_BAD_IN_NAME = re.compile(r'[<>:"/\\|?*\x00-\x1f\x7f]')
_DISPOSITION_UTF8 = re.compile(r"filename\*\s*=\s*(?:UTF-8|utf-8)''([^;]+)")
_DISPOSITION = re.compile(r'filename\s*=\s*"([^"]+)"|filename\s*=\s*([^;]+)')

Opener = Callable[[urllib.request.Request, float], "http.client.HTTPResponse"]


def _open(request: urllib.request.Request, timeout: float):
    return urllib.request.urlopen(request, timeout=timeout)


class WebError(Exception):
    """kind: gone (nothing there: the link expired or is wrong), denied (it wants a login), page (a web
    page, not a film), server (the server's own trouble), network (no answer, or cut off), disk (no room),
    torrent (not a .torrent file), stopped (we stopped it)."""

    def __init__(self, kind: str, detail: str = ""):
        super().__init__(f"{kind}: {detail}" if detail else kind)
        self.kind = kind
        self.detail = detail


@dataclass(frozen=True)
class WebLink:
    url: str
    host: str          # lower case, for showing and logging (never the whole link)
    name: str          # the file name in the address ("" if there's none)
    torrent: bool      # the address ends in .torrent


@dataclass(frozen=True)
class Info:
    name: str          # from the server (Content-Disposition) or the address ("" if neither says)
    size: int          # bytes; 0 = the server didn't say
    kind: str          # its Content-Type, lower case, without parameters
    ranges: bool       # the server can start in the middle (seeking while streaming, continuing a download)


def clean_name(name: str) -> str:
    """A file name Windows accepts: no path, no reserved characters, at most 200 characters."""
    name = _BAD_IN_NAME.sub("_", name or "").strip(" .")
    return name[:200].strip(" .")


def name_from_path(path: str) -> str:
    return clean_name(unquote(path.rstrip("/").rsplit("/", 1)[-1]))


def name_from_disposition(header: str) -> str:
    match = _DISPOSITION_UTF8.search(header or "")
    if match:
        return clean_name(unquote(match.group(1).strip().strip('"')))
    match = _DISPOSITION.search(header or "")
    return clean_name((match.group(1) or match.group(2) or "").strip()) if match else ""


# what to call a film whose name has no extension, by what the server says it is
EXTENSIONS = {"video/x-matroska": ".mkv", "video/mp4": ".mp4", "video/webm": ".webm", "video/quicktime": ".mov",
              "video/x-msvideo": ".avi", "video/mp2t": ".ts", "video/mpeg": ".mpg"}


def film_name(info: Info, link: WebLink) -> str:
    """The name the film plays and is saved under: the server's, else the address's, else "Film from <host>"."""
    name = info.name or link.name or f"Film from {link.host}"
    if Path(name).suffix.lower() not in VIDEO_EXTENSIONS and info.kind in EXTENSIONS:
        name += EXTENSIONS[info.kind]
    return clean_name(name)


def parse(text: str) -> Optional[WebLink]:
    text = (text or "").strip()
    if not text or len(text) > MAX_LINK or any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in text):
        return None
    try:
        parts = urlsplit(text)
        parts.port                                    # (a bad port number raises ValueError)
    except ValueError:
        return None
    if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
        return None
    name = name_from_path(parts.path)
    return WebLink(text, parts.hostname.lower(), name, name.lower().endswith(".torrent"))


def room_ok(url: str) -> bool:
    """A link from the room (the host's) may be opened: https, no password in it, and a host on the
    internet, not on this PC or the home network (a router's page is no film)."""
    link = parse(url)
    if link is None or not url.strip().lower().startswith("https://"):
        return False
    parts = urlsplit(url.strip())
    if parts.username is not None or parts.password is not None:
        return False
    host = link.host.rstrip(".")
    if host == "localhost" or host.endswith(LOCAL_SUFFIXES):
        return False
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return "." in host                             # a name ("nas" alone is on the home network)
    return address.is_global


def _http_kind(code: int) -> str:
    if code == 401:
        return "denied"
    if code in (403, 404, 410, 451):
        return "gone"
    return "server" if code >= 500 else "gone"


def _network(exc: BaseException) -> WebError:
    reason = getattr(exc, "reason", exc)
    return WebError("network", reason if isinstance(reason, str) else type(reason).__name__)


def probe(url: str, opener: Opener = _open) -> Info:
    """What's at the link, asked from the server: HEAD, or (servers that refuse HEAD) the first byte."""
    for method, extra in (("HEAD", {}), ("GET", {"Range": "bytes=0-0"})):
        request = urllib.request.Request(url, method=method, headers=dict(extra, **{"User-Agent": USER_AGENT}))
        try:
            with opener(request, TIMEOUT_S) as answer:
                return _info(answer, method)
        except urllib.error.HTTPError as exc:
            exc.close()
            if method == "HEAD" and exc.code in (400, 403, 405, 500, 501):
                continue                               # some servers only answer GET
            raise WebError(_http_kind(exc.code), f"HTTP {exc.code}") from None
        except (urllib.error.URLError, http.client.HTTPException, OSError, ValueError) as exc:
            raise _network(exc) from None
    raise WebError("server", "no answer to HEAD or GET")


def _info(answer, method: str) -> Info:
    headers = answer.headers
    kind = (headers.get("Content-Type") or "").split(";")[0].strip().lower()
    if kind in ("text/html", "application/xhtml+xml"):
        raise WebError("page", kind)
    size = 0
    if method == "GET" and answer.status == 206:
        total = (headers.get("Content-Range") or "").rpartition("/")[2].strip()
        size = int(total) if total.isdigit() else 0
    else:
        length = (headers.get("Content-Length") or "").strip()
        size = int(length) if length.isdigit() else 0
    ranges = answer.status == 206 or (headers.get("Accept-Ranges") or "").strip().lower() == "bytes"
    name = name_from_disposition(headers.get("Content-Disposition") or "") or \
        name_from_path(urlsplit(answer.geturl() or "").path)
    return Info(name, size, kind, ranges)


def fetch_torrent(url: str, opener: Opener = _open) -> bytes:
    """A .torrent file from a link: at most 20 MB, and it must look like one (a bencoded dictionary
    with an "info" part); libtorrent checks the rest."""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with opener(request, TIMEOUT_S) as answer:
            data = answer.read(MAX_TORRENT_FILE + 1)
    except urllib.error.HTTPError as exc:
        exc.close()
        raise WebError(_http_kind(exc.code), f"HTTP {exc.code}") from None
    except (urllib.error.URLError, http.client.HTTPException, OSError, ValueError) as exc:
        raise _network(exc) from None
    if len(data) > MAX_TORRENT_FILE or not data.startswith(b"d") or b"4:info" not in data:
        raise WebError("torrent", f"{len(data)} bytes, not a .torrent file")
    return data


class Download:
    """Saves the film at a link into ``folder``: as "<name>.sw-download" until it's complete, then under
    its name (or "<name> (2)" etc. if that's taken). Stopped halfway, it continues where it was the next
    time, also from a new link to the same file (Real-Debrid hands out new ones), as long as the server
    allows starting in the middle and says the same size."""

    def __init__(self, url: str, folder: Path, name: str, size: int, opener: Opener = _open):
        self.url, self.folder, self.name, self.size = url, folder, clean_name(name) or "Film", max(0, int(size))
        self._open = opener
        self.part = folder / (self.name + PART)
        self.notes = folder / (self.name + PART + ".json")

    def already(self) -> int:
        """Bytes saved earlier for this film (0 if none, or if they belong to another file)."""
        try:
            notes = json.loads(self.notes.read_text(encoding="utf-8"))
            have = self.part.stat().st_size
        except (OSError, ValueError, AttributeError):
            return 0
        same = isinstance(notes, dict) and self.size > 0 and notes.get("size") == self.size
        return have if same and have <= self.size else 0

    def run(self, progress: Callable[[int, int], None], stopped: Callable[[], bool]) -> Path:
        """Downloads (blocking; for a background thread). progress(have, size) about twice a second.
        Returns where the complete film is. Raises WebError (kind "stopped" when stopped() said so)."""
        self.folder.mkdir(parents=True, exist_ok=True)
        have = self.already()
        if self.size and space_problem(self.folder, self.size - have):
            raise WebError("disk", f"{self.size - have} bytes needed")
        headers = {"User-Agent": USER_AGENT}
        if have:
            headers["Range"] = f"bytes={have}-"
        self.notes.write_text(json.dumps({"size": self.size}), encoding="utf-8")
        try:
            answer = self._open(urllib.request.Request(self.url, headers=headers), TIMEOUT_S)
        except urllib.error.HTTPError as exc:
            exc.close()
            if exc.code == 416 and have and have == self.size:
                return self._finish(have, progress)    # it was all there already
            raise WebError(_http_kind(exc.code), f"HTTP {exc.code}") from None
        except (urllib.error.URLError, http.client.HTTPException, OSError, ValueError) as exc:
            raise _network(exc) from None
        with answer:
            if have and answer.status != 206:
                have = 0                               # the server starts from the beginning again
            try:
                out = open(self.part, "ab" if have else "wb")
            except OSError as exc:
                raise WebError("disk", type(exc).__name__) from None
            with out:
                last = 0.0
                while True:
                    if stopped():
                        raise WebError("stopped")
                    try:
                        chunk = answer.read(CHUNK)
                    except (http.client.HTTPException, OSError, ValueError) as exc:     # (cut off)
                        raise _network(exc) from None
                    if not chunk:
                        break
                    try:
                        out.write(chunk)
                    except OSError as exc:                                         # (the drive is full)
                        raise WebError("disk", type(exc).__name__) from None
                    have += len(chunk)
                    if self.size and have > self.size:
                        raise WebError("server", "more than it said")
                    if time.monotonic() - last >= 0.5:
                        last = time.monotonic()
                        progress(have, self.size)
        if self.size and have < self.size:
            raise WebError("network", "cut off")
        return self._finish(have, progress)

    def _finish(self, have: int, progress: Callable[[int, int], None]) -> Path:
        progress(have, self.size or have)
        target = unique_path(self.folder, self.name)
        try:
            os.replace(self.part, target)
        except OSError as exc:
            raise WebError("disk", type(exc).__name__) from None
        try:
            self.notes.unlink()
        except OSError:
            pass
        return target
