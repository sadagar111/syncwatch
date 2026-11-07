"""Streaming a torrent in the window: the helper process, its progress, and handing the film to the player.

One torrent at a time. torrent/engine.py runs as its own process (libtorrent is big C++; if it
crashes, SyncWatch doesn't). It tells the window what's in the torrent, SyncWatch picks the film
(the biggest video file), and once the first few MB are there the player opens the helper's local
address. While it plays, the control bar shows how far the download is.
"""
from __future__ import annotations

import importlib.util
import json
import logging
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from PySide6.QtCore import QObject, QProcess, Signal

from .. import errors
from ..torrent import engine as engine_module, links
from ..units import human_size, human_time

log = logging.getLogger("syncwatch.torrent")

PROBLEMS = {"bad_source": "SW-620", "no_peers": "SW-621", "no_video": "SW-622", "disk": "SW-623",
            "missing": "SW-624", "other": "SW-625", "peer": ""}


def libtorrent_installed() -> bool:
    return importlib.util.find_spec("libtorrent") is not None


@dataclass
class TorrentSource:
    """What the player opens: the helper's local address of the chosen file (see youtube/streams.py)."""
    id: str                 # the torrent's info-hash
    title: str              # the file's name
    url: str
    size: int
    headers: Dict[str, str] = field(default_factory=dict)
    kind = "torrent"
    problem = "SW-626"

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
        return f"torrent, {human_size(self.size)}"


def status_text(status: Dict, started: float) -> str:
    """'⬇ 45 % · 3.2 MB/s · 18 people · 2 min 05 s left' for the control bar."""
    if status.get("state") in ("downloading_metadata", "checking_resume_data", "checking_files"):
        return f"⬇ finding the torrent · {status.get('peers', 0)} people"
    progress = float(status.get("progress", 0.0))
    if progress >= 1.0:
        return "⬇ downloaded completely"
    parts = [f"⬇ {progress * 100:.0f} %", f"{human_size(status.get('down', 0))}/s",
             f"{status.get('peers', 0)} people"]
    rate, left = status.get("down", 0), status.get("size", 0) - status.get("have", 0)
    if rate > 0 and left > 0 and time.monotonic() - started > 5:
        parts.append(human_time(left / rate))
    return " · ".join(parts)


class TorrentEngine(QObject):
    """The helper process: commands in, events out (JSON lines)."""
    event = Signal(dict)
    stopped = Signal()

    def __init__(self, arguments: List[str], parent=None):
        super().__init__(parent)
        self._arguments = arguments
        self._buffer = b""
        self._pending: List[bytes] = []          # commands sent before the process is running
        self.process = QProcess(self)
        self.process.setProcessChannelMode(QProcess.ProcessChannelMode.SeparateChannels)
        self.process.readyReadStandardOutput.connect(self._read)
        self.process.readyReadStandardError.connect(self._read_errors)
        self.process.finished.connect(lambda *_: self.stopped.emit())
        self.process.errorOccurred.connect(self._error)
        self.process.started.connect(self._flush)

    def start(self) -> None:
        program, *options = engine_module.command(*self._arguments)
        self.process.start(program, options)

    def send(self, **command) -> None:
        line = (json.dumps(command) + "\n").encode("ascii")
        if self.process.state() == QProcess.ProcessState.Running:
            self.process.write(line)
        elif self.process.state() == QProcess.ProcessState.Starting:
            self._pending.append(line)           # start() returns before the process runs

    def _flush(self) -> None:
        for line in self._pending:
            self.process.write(line)
        self._pending.clear()

    def stop(self, delete: bool) -> None:
        if self.process.state() == QProcess.ProcessState.NotRunning:
            return
        self.send(cmd="quit", delete=delete)
        self.process.closeWriteChannel()
        if not self.process.waitForFinished(8000):
            self.process.kill()

    def _read(self) -> None:
        self._buffer += bytes(self.process.readAllStandardOutput())
        *lines, self._buffer = self._buffer.split(b"\n")
        for line in lines:
            try:
                message = json.loads(line)
            except ValueError:
                continue
            if isinstance(message, dict) and isinstance(message.get("ev"), str):
                self.event.emit(message)

    def _read_errors(self) -> None:
        text = bytes(self.process.readAllStandardError()).decode("utf-8", "replace").strip()
        if text:
            log.debug("torrent helper: %s", text[-2000:])

    def _error(self, error) -> None:
        if error == QProcess.ProcessError.FailedToStart:
            self.event.emit({"ev": "error", "kind": "missing", "message": self.process.errorString()})


class Torrents(QObject):
    """The window's torrent part. ``ready(TorrentSource)`` when the film can start playing.
    Also sharing a film with the room: ``shared(magnet, name, size)`` once others can fetch it."""
    ready = Signal(object)
    statusText = Signal(str)        # for the control bar ("" = nothing streaming)
    message = Signal(str, str)      # (text, kind) for the activity feed
    shared = Signal(str, str, int)
    addressesChanged = Signal(list)

    def __init__(self, settings, default_folder, parent=None, cache: Optional[Path] = None, library=None):
        super().__init__(parent)
        self.settings = settings
        self.default_folder = default_folder        # a function: the library's Torrents folder
        self.cache = cache                          # descriptions of films shared before
        self.library = library                      # a function: the library folder (finished films go there)
        self.mode = ""                              # "stream" or "share"
        self.addresses: List[str] = []
        self.done = False
        self._share_info: Optional[tuple] = None
        self._hashing_started = 0.0
        self.engine: Optional[TorrentEngine] = None
        self.source_text = ""
        self.hash = ""
        self.name = ""
        self.files: List[Dict] = []
        self.chosen: Optional[Dict] = None
        self.playing: Optional[TorrentSource] = None
        self.started = 0.0
        self.last_status: Dict = {}

    @property
    def active(self) -> bool:
        return self.engine is not None

    def folder(self) -> Path:
        chosen = self.settings.get("torrents.folder")
        return Path(chosen) if chosen else Path(self.default_folder())

    def open(self, source: str, private: bool = False) -> bool:
        """A magnet link or a .torrent file's path. Stops whatever streamed before.
        private: a film shared in the room (announced nowhere, only fetched from those in the room)."""
        source = source.strip()
        if not (links.is_magnet(source) or links.is_torrent_file(Path(source))):
            errors.report("SW-620", source[:200])
            return False
        if not libtorrent_installed():
            errors.report("SW-624", "libtorrent not found")
            return False
        self.stop()
        self._start_engine("stream")
        self.engine.send(cmd="add", source=source, private=private)
        self.source_text = source
        self.hash = links.magnet_hash(source) or ""
        self.name = links.magnet_name(source) or (Path(source).stem if not links.is_magnet(source) else "")
        self.message.emit(f"Starting the torrent {self.name or self.hash[:12]}…", "you")
        self.statusText.emit("⬇ finding the torrent…")
        return True

    def share(self, path: Path) -> bool:
        """Offer a film on this PC to the room (a private torrent; see torrent/engine.py)."""
        if not libtorrent_installed():
            errors.report("SW-624", "libtorrent not found")
            return False
        self.stop()
        self._start_engine("share")
        self.name, self.source_text, self.hash = path.name, "", ""
        self._hashing_started = time.monotonic()
        self.engine.send(cmd="share", path=str(path))
        self.message.emit(f"Preparing {path.name} for sharing…", "you")
        return True

    def _start_engine(self, mode: str) -> None:
        listen = "0.0.0.0:0,[::]:0" if self.settings.get("torrents.incoming") else "127.0.0.1:0"
        arguments = ["--folder", str(self.folder()), "--listen", listen,
                     "--upload-kbps", str(int(self.settings.get("torrents.upload_kbps"))),
                     "--connections", str(int(self.settings.get("torrents.connections"))),
                     "--dht", "yes" if self.settings.get("torrents.dht") else "no",
                     "--start-mb", str(int(self.settings.get("torrents.start_mb")))]
        if self.cache is not None:
            arguments += ["--cache", str(self.cache)]
        self.engine = TorrentEngine(arguments, self)
        self.engine.event.connect(self._event)
        self.engine.stopped.connect(self._engine_stopped)
        self.mode, self.done, self.addresses, self._share_info = mode, False, [], None
        self.files, self.chosen, self.playing, self.last_status = [], None, None, {}
        self.started = time.monotonic()
        self.engine.start()

    def magnet(self) -> str:
        """A magnet link for what streams now: the one opened, or one made from the info-hash."""
        if links.is_magnet(self.source_text):
            return self.source_text
        if len(self.hash) == 40:
            from urllib.parse import quote  # noqa: PLC0415
            return f"magnet:?xt=urn:btih:{self.hash}&dn={quote(self.name)}"
        return ""

    def add_peer(self, address: str) -> None:
        """Someone who has the film (e.g. a friend in the room): connect to them directly."""
        if self.engine is not None:
            self.engine.send(cmd="peer", address=address)

    def stop(self) -> None:
        if self.engine is None:
            return
        engine, self.engine = self.engine, None
        engine.stopped.disconnect(self._engine_stopped)
        delete = self.mode == "stream" and self.settings.get("torrents.after") == "delete"
        engine.stop(delete=delete)   # never deletes a film that was only shared
        engine.deleteLater()
        if self.mode == "stream" and self.done and not delete:
            self._into_library()
        self.playing = None
        self.mode = ""
        self.statusText.emit("")

    def _into_library(self) -> None:
        """A completely downloaded film moves from the download folder into the library."""
        if not (self.settings.get("torrents.into_library") and self.library and self.chosen):
            return
        from ..library import films as library_films  # noqa: PLC0415
        source = self.folder() / self.chosen["path"]
        if not engine_module.is_video(source.name) or not source.resolve().is_relative_to(self.folder().resolve()):
            log.warning("not moving %s into the library (not a film in the download folder)", source)
            return
        try:
            target = library_films.unique_path(Path(self.library()), source.name)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(source), str(target))   # a rename on the same drive, else a copy
        except OSError as exc:
            log.warning("couldn't move %s into the library: %s", source, exc)
            return
        self.message.emit(f"{target.name} is in your library now", "info")

    def _engine_stopped(self) -> None:
        if self.engine is not None and self.sender() is self.engine:
            self.engine = None
            self.statusText.emit("")
            if self.playing is None:
                errors.report("SW-625", "the torrent helper stopped")

    def _event(self, message: Dict) -> None:
        kind = message["ev"]
        if kind == "files":
            self._files(message)
        elif kind == "ready":
            self._ready(message)
        elif kind == "hashing":
            total, done = max(1, int(message.get("total", 1))), int(message.get("done", 0))
            took = time.monotonic() - self._hashing_started
            left = f" · {human_time(took / done * (total - done))}" if done and took > 2 else ""
            self.statusText.emit(f"⬆ preparing {self.name} for sharing: {done * 100 // total} %{left}")
        elif kind == "shared":
            self._share_info = (str(message.get("magnet", "")), str(message.get("name", self.name)),
                                int(message.get("size", 0)))
            self.hash = str(message.get("hash", ""))
        elif kind == "addresses":
            self.addresses = [a for a in message.get("list", []) if isinstance(a, str) and links.valid_address(a)]
            self.addressesChanged.emit(list(self.addresses))
            if self._share_info is not None:     # shared: tell the room (and again when addresses change)
                self.shared.emit(*self._share_info)
        elif kind == "status":
            self.last_status = message
            if message.get("sharing"):
                self.statusText.emit(f"⬆ sharing {self.name} · {human_size(message.get('up', 0))}/s · "
                                     f"{message.get('peers', 0)} people")
            else:
                self.statusText.emit(status_text(message, self.started))
        elif kind == "done":
            self.done = True
            self.message.emit(f"{self.chosen['path'] if self.chosen else 'The film'} is downloaded completely", "info")
        elif kind == "error":
            code = PROBLEMS.get(message.get("kind", "other"), "SW-625")
            log.warning("torrent: %s: %s", message.get("kind"), message.get("message"))
            if code:
                errors.report(code, str(message.get("message", ""))[:500])
                if code in ("SW-620", "SW-621", "SW-622", "SW-623", "SW-624"):
                    self.stop()

    def _files(self, message: Dict) -> None:
        self.name = str(message.get("name", ""))[:200] or self.name
        self.hash = str(message.get("hash", ""))[:80] or self.hash
        self.files = [f for f in message.get("files", []) if isinstance(f, dict)]
        index = engine_module.best_video(self.files)
        if index is None:
            return   # the helper reports "no video" itself
        self.chosen = next(f for f in self.files if f["index"] == index)
        others = len([f for f in self.files if engine_module.is_video(f["path"])]) - 1
        extra = f" (the biggest of {others + 1} videos)" if others else ""
        self.message.emit(f"Streaming {Path(self.chosen['path']).name}{extra}, {human_size(self.chosen['size'])}", "you")
        self.engine.send(cmd="select", index=index)

    def _ready(self, message: Dict) -> None:
        url = str(message.get("url", ""))
        if not url.startswith("http://127.0.0.1:"):
            return
        self.playing = TorrentSource(self.hash, str(message.get("name", self.name))[:200], url,
                                     int(message.get("size", 0)))
        self.ready.emit(self.playing)
