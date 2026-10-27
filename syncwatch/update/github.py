"""Finding the newest release on GitHub and downloading its files.

Normal versions are found through github.com's "latest release" link, which
doesn't use GitHub's API at all. Test versions (pre-releases) need the API
list of releases, which GitHub limits to 60 requests per hour per internet
connection. SyncWatch checks at most every few hours, so that's plenty.

Large downloads resume where they stopped (after a network hiccup or a
restart), report progress with speed and time left, and are checked against
the size and SHA-256 fingerprint from the signed manifest.
"""
from __future__ import annotations

import hashlib
import http.client
import json
import logging
import os
import socket
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional, Tuple

from .. import __version__
from .version import Version, parse as parse_version

log = logging.getLogger("syncwatch.update")

API_BASE = "https://api.github.com"
WEB_BASE = "https://github.com"
TIMEOUT_S = 20
CHUNK = 256 * 1024
RETRIES = 3

Progress = Callable[[int, int, float, Optional[float]], None]   # done, total, bytes/s, seconds left


class NetError(Exception):
    """Couldn't reach GitHub (SW-701) or GitHub refused (rate limit: SW-702)."""

    def __init__(self, code: str, technical: str):
        super().__init__(technical)
        self.code = code
        self.technical = technical


class NotFound(NetError):
    def __init__(self, technical: str):
        super().__init__("SW-701", technical)


class ChecksumError(Exception):
    """The downloaded file isn't what the signed manifest describes (SW-705)."""


class Cancelled(Exception):
    pass


@dataclass(frozen=True)
class ReleaseFiles:
    tag: str
    prerelease: bool
    manifest: bytes
    signature: bytes


def _check_url(url: str) -> None:
    parsed = urllib.parse.urlparse(url)
    local = parsed.hostname in ("127.0.0.1", "localhost")
    if parsed.scheme != "https" and not (parsed.scheme == "http" and local):
        raise NetError("SW-701", f"refusing non-https URL {url}")


class _Redirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _check_url(newurl)  # never follow a redirect away from https
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class GitHub:
    def __init__(self, repo: str, api_base: str = API_BASE, web_base: str = WEB_BASE, timeout: float = TIMEOUT_S):
        self.repo = repo
        self.api_base = api_base.rstrip("/")
        self.web_base = web_base.rstrip("/")
        self.timeout = timeout
        context = ssl.create_default_context()  # Windows: uses the Windows certificate store
        self.opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=context), _Redirects())

    # ------------------------------------------------------------ low level
    def _open(self, url: str, headers: Optional[dict] = None):
        _check_url(url)
        request = urllib.request.Request(url, headers={"User-Agent": f"SyncWatch/{__version__}", **(headers or {})})
        try:
            return self.opener.open(request, timeout=self.timeout)
        except urllib.error.HTTPError as exc:
            remaining = exc.headers.get("X-RateLimit-Remaining") if exc.headers else None
            if exc.code == 404:
                raise NotFound(f"HTTP 404 for {url}") from exc
            if exc.code == 429 or (exc.code == 403 and remaining == "0"):
                raise NetError("SW-702", f"HTTP {exc.code} rate limited ({url})") from exc
            raise NetError("SW-701", f"HTTP {exc.code} {exc.reason} ({url})") from exc
        except ssl.SSLError as exc:
            raise NetError("SW-701", f"secure connection failed: {exc} ({url})") from exc
        except urllib.error.URLError as exc:
            reason = exc.reason
            if isinstance(reason, ssl.SSLError):
                raise NetError("SW-701", f"secure connection failed: {reason} ({url})") from exc
            raise NetError("SW-701", f"{type(reason).__name__}: {reason} ({url})") from exc
        except (socket.timeout, TimeoutError, ConnectionError, http.client.HTTPException, OSError) as exc:
            raise NetError("SW-701", f"{type(exc).__name__}: {exc} ({url})") from exc

    def fetch(self, url: str, max_bytes: int, headers: Optional[dict] = None) -> bytes:
        with self._open(url, headers) as response:
            data = response.read(max_bytes + 1)
        if len(data) > max_bytes:
            raise NetError("SW-701", f"response larger than {max_bytes} bytes ({url})")
        return data

    def asset_url(self, tag: str, name: str) -> str:
        return f"{self.web_base}/{self.repo}/releases/download/{urllib.parse.quote(tag)}/{urllib.parse.quote(name)}"

    # ------------------------------------------------------------ finding the newest release
    def newest(self, include_test: bool, max_manifest: int) -> Optional[ReleaseFiles]:
        """The newest release's manifest and signature, or None if nothing is published yet."""
        if include_test:
            return self._newest_from_api(max_manifest)
        try:
            manifest = self.fetch(f"{self.web_base}/{self.repo}/releases/latest/download/manifest.json", max_manifest)
            signature = self.fetch(f"{self.web_base}/{self.repo}/releases/latest/download/manifest.json.sig", 4096)
        except NotFound:
            return None
        return ReleaseFiles("latest", False, manifest, signature)

    def _newest_from_api(self, max_manifest: int) -> Optional[ReleaseFiles]:
        data = self.fetch(f"{self.api_base}/repos/{self.repo}/releases?per_page=20", 2_000_000,
                          {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"})
        try:
            releases = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise NetError("SW-701", f"GitHub sent invalid data: {exc}") from exc
        candidates: List[Tuple[Version, dict]] = []
        for release in releases if isinstance(releases, list) else []:
            if not isinstance(release, dict) or release.get("draft"):
                continue
            version = parse_version(str(release.get("tag_name", "")))
            names = {a.get("name") for a in release.get("assets", []) if isinstance(a, dict)}
            if version is not None and {"manifest.json", "manifest.json.sig"} <= names:
                candidates.append((version, release))
        if not candidates:
            return None
        version, release = max(candidates, key=lambda item: item[0])
        tag = release["tag_name"]
        manifest = self.fetch(self.asset_url(tag, "manifest.json"), max_manifest)
        signature = self.fetch(self.asset_url(tag, "manifest.json.sig"), 4096)
        return ReleaseFiles(tag, bool(release.get("prerelease")), manifest, signature)

    # ------------------------------------------------------------ big downloads
    def download(self, url: str, dest: Path, size: int, sha256: str, progress: Optional[Progress] = None,
                 cancel: Optional[threading.Event] = None) -> Path:
        """Download to ``dest`` (resuming a ``.part`` file), verify size and SHA-256.

        Raises NetError (SW-704 when the download breaks off), ChecksumError, Cancelled.
        """
        dest.parent.mkdir(parents=True, exist_ok=True)
        part = dest.with_name(dest.name + ".part")
        if dest.exists() and dest.stat().st_size == size and _sha256(dest) == sha256:
            if progress:
                progress(size, size, 0.0, 0.0)
            return dest  # already downloaded earlier: don't download again
        last_error: Optional[Exception] = None
        for attempt in range(RETRIES):
            try:
                self._download_once(url, part, size, progress, cancel)
                break
            except ChecksumError:
                if part.exists():
                    part.unlink()
                raise
            except NetError as exc:
                last_error = exc
                log.info("download attempt %d failed: %s", attempt + 1, exc.technical)
                if cancel is not None and cancel.wait(2.0 * (attempt + 1)):
                    raise Cancelled()
        else:
            raise NetError("SW-704", getattr(last_error, "technical", str(last_error)))
        if part.stat().st_size != size or _sha256(part) != sha256:
            part.unlink()
            raise ChecksumError(f"{dest.name}: size or SHA-256 doesn't match the signed manifest")
        os.replace(part, dest)
        return dest

    def _download_once(self, url: str, part: Path, size: int, progress: Optional[Progress],
                       cancel: Optional[threading.Event]) -> None:
        have = part.stat().st_size if part.exists() else 0
        if have > size:
            part.unlink()
            have = 0
        if have == size:
            return
        headers = {"Accept": "application/octet-stream"}
        if have:
            headers["Range"] = f"bytes={have}-"
        response = self._open(url, headers)
        with response:
            status = getattr(response, "status", 200)
            if have and status != 206:
                have = 0  # the server ignored the resume request: start over
            mode = "ab" if have else "wb"
            started, done = time.monotonic(), have
            speed = 0.0
            last_report = 0.0
            with open(part, mode) as handle:
                while True:
                    if cancel is not None and cancel.is_set():
                        raise Cancelled()
                    try:
                        chunk = response.read(CHUNK)
                    except (socket.timeout, TimeoutError, ConnectionError, http.client.HTTPException, OSError) as exc:
                        raise NetError("SW-704", f"{type(exc).__name__}: {exc}") from exc
                    if not chunk:
                        break
                    handle.write(chunk)
                    done += len(chunk)
                    if done > size:
                        raise ChecksumError(f"server sent more data than the signed manifest says ({size} bytes)")
                    now = time.monotonic()
                    elapsed = max(1e-3, now - started)
                    speed = (done - have) / elapsed
                    if progress and now - last_report > 0.2:
                        last_report = now
                        eta = (size - done) / speed if speed > 0 else None
                        progress(done, size, speed, eta)
        if done < size:
            raise NetError("SW-704", f"download ended early ({done} of {size} bytes)")
        if progress:
            progress(size, size, speed, 0.0)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()
