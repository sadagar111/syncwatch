"""Films from web links in the window (since 0.5.1): what the player opens, the download beside it,
and the progress for the control bar.

A link plays straight from its address (mpv streams it and can jump anywhere when the server allows
starting in the middle). With "Download it while it plays" the film is also saved, in the background,
into the download folder (Settings > Downloads & library), and moves into the library once it's complete; the
player keeps playing from the link meanwhile. A link to a .torrent file goes to the torrent player.
The work happens in a background thread; the signals arrive in the window's thread.
"""
from __future__ import annotations

import hashlib
import logging
import shutil
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Deque, Dict, List, Optional, Tuple

from PySide6.QtCore import QObject, Signal

from ..library import weblinks
from ..library.weblinks import WebError, WebLink
from ..units import human_size, human_time

log = logging.getLogger("syncwatch.weblinks")

PROBLEMS = {"gone": "SW-640", "denied": "SW-641", "page": "SW-643", "network": "SW-645", "server": "SW-645",
            "disk": "SW-646", "torrent": "SW-647"}
RETRIES = 3                  # a download that's cut off continues by itself this often
RETRY_WAIT_S = 5.0


@dataclass
class WebSource:
    """What the player opens: the film at a web link (see youtube/streams.py for the other kinds)."""
    id: str                 # "web:" + a short hash of the link (the link itself is never logged)
    title: str              # the file's name
    url: str
    size: int               # 0 = the server didn't say
    host: str
    headers: Dict[str, str] = field(default_factory=dict)
    kind = "web"
    problem = "SW-642"

    @property
    def label(self) -> str:
        return self.title

    @property
    def duration_ms(self) -> int:
        return 0             # mpv tells

    def address(self) -> str:
        return self.url

    def header_fields(self) -> List[str]:
        return []

    def describe(self) -> str:
        return f"web link from {self.host}, {human_size(self.size) if self.size else 'size unknown'}"


def source_id(url: str) -> str:
    return "web:" + hashlib.sha256(url.encode("utf-8")).hexdigest()[:12]


def download_text(have: int, size: int, rate: float, running_s: float) -> str:
    """'⬇ 45 % · 12 MB/s · 2 min 05 s left' for the control bar."""
    if size and have >= size:
        return "⬇ downloaded completely"
    parts = [f"⬇ {have * 100 // size} %" if size else f"⬇ {human_size(have)}"]
    if rate > 0:
        parts.append(f"{human_size(rate)}/s")
        if size and running_s > 3:
            parts.append(f"{human_time((size - have) / rate)} left")
    return " · ".join(parts)


class Speed:
    """Download speed over the last few seconds."""

    def __init__(self, window_s: float = 5.0):
        self.window_s = window_s
        self.samples: Deque[Tuple[float, int]] = deque()

    def add(self, have: int) -> float:
        now = time.monotonic()
        self.samples.append((now, have))
        while len(self.samples) > 2 and now - self.samples[0][0] > self.window_s:
            self.samples.popleft()
        (first_t, first_have), (last_t, last_have) = self.samples[0], self.samples[-1]
        return (last_have - first_have) / (last_t - first_t) if last_t > first_t else 0.0


class WebLinks(QObject):
    ready = Signal(object)          # a WebSource: play it
    torrentReady = Signal(str)      # a .torrent file saved from a link: open it as a torrent
    failed = Signal(str, str)       # (problem code, detail for the log: never the link)
    statusText = Signal(str)        # the control bar: the download's progress ("" = nothing to show)
    message = Signal(str, str)      # (text, kind) for the activity line

    def __init__(self, folder: Callable[[], Path], library: Callable[[], Optional[Path]],
                 into_library: Callable[[], bool], parent=None):
        super().__init__(parent)
        self._folder, self._library, self._into_library = folder, library, into_library
        self._lock = threading.Lock()
        self._generation = 0
        self._stop = threading.Event()
        self.url = ""                    # the link playing now ("" if none)
        self.downloading = False

    # ------------------------------------------------------------ what the window asks
    def open(self, link: WebLink, download: bool) -> None:
        """Play the film at the link (found out in the background), and save it too if download."""
        generation, stop = self._restart()
        self.url = link.url
        threading.Thread(target=self._play, args=(generation, stop, link, download), daemon=True,
                         name="web-link").start()

    def fetch_torrent(self, link: WebLink) -> None:
        generation, _stop = self._restart()
        threading.Thread(target=self._torrent, args=(generation, link), daemon=True, name="web-torrent").start()

    def stop(self) -> None:
        """Something else plays now: stop the download (what's saved stays, to continue later)."""
        self._restart()
        self.url = ""

    # ------------------------------------------------------------ in the background
    def _restart(self) -> Tuple[int, threading.Event]:
        with self._lock:
            self._stop.set()
            self._stop = threading.Event()
            self._generation += 1
            if self.downloading:
                self.downloading = False
                self.statusText.emit("")
            return self._generation, self._stop

    def _current(self, generation: int) -> bool:
        with self._lock:
            return generation == self._generation

    def _play(self, generation: int, stop: threading.Event, link: WebLink, download: bool) -> None:
        try:
            info = weblinks.probe(link.url)
        except WebError as exc:
            with self._lock:
                if generation != self._generation:
                    return
                self.url = ""              # (nothing plays from it: the same link may be tried again)
            log.info("web link from %s: %s", link.host, exc)
            self.failed.emit(PROBLEMS.get(exc.kind, "SW-645"), str(exc))
            return
        if not self._current(generation):
            return
        name = weblinks.film_name(info, link)
        log.info("web link from %s: %s, %s, %s", link.host, info.kind or "no type",
                 human_size(info.size) if info.size else "size unknown",
                 "can jump" if info.ranges else "can't jump (the server starts from the beginning)")
        self.ready.emit(WebSource(source_id(link.url), name, link.url, info.size, link.host))
        if download:
            self._download(generation, stop, link, name, info.size)

    def _download(self, generation: int, stop: threading.Event, link: WebLink, name: str, size: int) -> None:
        folder = self._folder()
        job = weblinks.Download(link.url, folder, name, size)
        with self._lock:
            if generation != self._generation:
                return
            self.downloading = True
        speed, started = Speed(), time.monotonic()
        earlier = job.already()
        if earlier:
            self.message.emit(f"Continuing the download of {name} ({human_size(earlier)} saved before)", "info")

        def progress(have: int, total: int) -> None:
            if self._current(generation):
                self.statusText.emit(download_text(have, total, speed.add(have), time.monotonic() - started))

        for attempt in range(RETRIES + 1):
            try:
                path = job.run(progress, stop.is_set)
                break
            except WebError as exc:
                if exc.kind == "stopped" or not self._current(generation):
                    return
                if exc.kind == "network" and attempt < RETRIES:
                    log.info("download from %s cut off (%s): continuing", link.host, exc)
                    if stop.wait(RETRY_WAIT_S):
                        return
                    continue
                log.info("download from %s failed: %s", link.host, exc)
                self._finished(generation)
                self.failed.emit("SW-646" if exc.kind == "disk" else "SW-644", str(exc))
                return
        self._finished(generation)
        self._move_into_library(path)

    def _finished(self, generation: int) -> None:
        with self._lock:
            if generation == self._generation and self.downloading:
                self.downloading = False
                self.statusText.emit("")

    def _move_into_library(self, path: Path) -> None:
        library = self._library()
        if not (self._into_library() and library):
            self.message.emit(f"{path.name} is downloaded (in {path.parent})", "info")
            return
        from ..library import films as library_films  # noqa: PLC0415
        try:
            target = library_films.unique_path(Path(library), path.name)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(path), str(target))      # a rename on the same drive, else a copy
        except OSError as exc:
            log.warning("couldn't move %s into the library: %s", path.name, exc)
            self.message.emit(f"{path.name} is downloaded (in {path.parent})", "info")
            return
        self.message.emit(f"{target.name} is downloaded and in your library now", "info")

    def _torrent(self, generation: int, link: WebLink) -> None:
        try:
            data = weblinks.fetch_torrent(link.url)
            folder = self._folder()
            folder.mkdir(parents=True, exist_ok=True)
            target = folder / (link.name if link.name.lower().endswith(".torrent") else "link.torrent")
            target.write_bytes(data)
        except WebError as exc:
            if self._current(generation):
                log.info(".torrent link from %s: %s", link.host, exc)
                self.failed.emit(PROBLEMS.get(exc.kind, "SW-645"), str(exc))
            return
        except OSError as exc:
            self.failed.emit("SW-646", type(exc).__name__)
            return
        if self._current(generation):
            self.torrentReady.emit(str(target))
