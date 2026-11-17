"""YouTube in the window: the link box, finding videos in the background, and their problems.

Finding a video runs youtube/resolve.py (yt-dlp) as its own process, like the converter. Only one
video is opened at a time: if someone pastes another link meanwhile, the newest wins. Videos that
were found are kept for a while (their addresses work for hours), so opening one again, or the
next video of a queue that was looked up ahead, starts straight away.
"""
from __future__ import annotations

import json
import logging
import threading
import time
import urllib.request
from urllib.parse import quote
from pathlib import Path
from typing import Callable, Dict, List, Optional

from PySide6.QtCore import QObject, QProcess, Qt, QTimer, Signal
from PySide6.QtWidgets import QDialog, QHBoxLayout, QLabel, QLineEdit, QPushButton, QVBoxLayout, QWidget

from .. import errors, privacy
from ..library import weblinks as web_links
from ..torrent import links as torrent_links
from ..youtube import links, resolve, streams
from ..youtube.parts import find_js_runtime, ytdlp_installed  # noqa: F401 - (tests replace them here)

log = logging.getLogger("syncwatch.youtube")

PROBLEMS = {"unavailable": "SW-602", "private": "SW-602", "signin": "SW-603", "bot": "SW-604",
            "outdated": "SW-605", "other": "SW-605", "network": "SW-606", "live": "SW-607",
            "missing": "SW-608"}
CACHE_SIZE = 50




class ResolveJob(QObject):
    """One run of resolve.py. Emits found(Stream) or failed(kind, message), exactly once."""
    found = Signal(object)
    failed = Signal(str, str)

    def __init__(self, video_id: str, arguments: list, timeout_s: int, parent=None):
        super().__init__(parent)
        self.video_id = video_id
        self.started_at = 0.0
        self._done = False
        self._arguments = arguments
        self.process = QProcess(self)
        self.process.setProcessChannelMode(QProcess.ProcessChannelMode.SeparateChannels)
        self.process.finished.connect(self._finished)
        self.process.errorOccurred.connect(self._error)
        self._timer = QTimer(self, singleShot=True, interval=max(10, timeout_s) * 1000)
        self._timer.timeout.connect(self._timed_out)

    def start(self) -> None:
        program, *options = resolve.command(self.video_id, *self._arguments)
        self.started_at = time.monotonic()
        self.process.start(program, options)
        self._timer.start()

    def cancel(self) -> None:
        self._done = True
        self._timer.stop()
        if self.process.state() != QProcess.ProcessState.NotRunning:
            self.process.kill()

    def _finish(self, kind: str, message: str, stream=None) -> None:
        if self._done:
            return
        self._done = True
        self._timer.stop()
        if stream is not None:
            self.found.emit(stream)
        else:
            self.failed.emit(kind, message)

    def _timed_out(self) -> None:
        self.process.kill()
        self._finish("network", "YouTube didn't answer in time")

    def _error(self, error) -> None:
        if error == QProcess.ProcessError.FailedToStart:
            self._finish("missing", f"couldn't start Python for yt-dlp: {self.process.errorString()}")

    def _finished(self, _code: int, _status) -> None:
        output = bytes(self.process.readAllStandardOutput()).decode("utf-8", "replace")
        trouble = bytes(self.process.readAllStandardError()).decode("utf-8", "replace").strip()
        if trouble:
            log.debug("resolve.py said on stderr: %s", trouble[-2000:])
        lines = [line for line in output.splitlines() if line.strip()]
        try:
            message = json.loads(lines[-1]) if lines else {}
        except ValueError:
            message = {}
        self._log_warnings(message)
        if "ok" in message:
            try:
                self._finish("", "", self._parse_ok(message["ok"]))
            except streams.BadStream as exc:
                self._finish("other", f"odd answer from resolve.py: {exc}")
            return
        kind = message.get("error") if isinstance(message.get("error"), str) else "other"
        text = message.get("message") if isinstance(message.get("message"), str) else (trouble[-1000:] or "no answer")
        self._finish(kind if kind in PROBLEMS else "other", text)


    def _parse_ok(self, raw):
        return streams.from_json(raw)

    def _log_warnings(self, message) -> None:
        """yt-dlp's warnings (resolve.py already took the addresses out; cleaned again here anyway):
        they say why YouTube's formats were skipped, which explains many failures."""
        if not isinstance(message, dict):
            return
        ok = message.get("ok") if isinstance(message.get("ok"), dict) else {}
        found = message.get("warnings") or ok.get("warnings") or []
        apps = [part.get("client") for part in (ok.get("video"), ok.get("audio")) if isinstance(part, dict)]
        apps = [app for app in apps if isinstance(app, str) and resolve.CLIENT_CODE_RE.match(app)]
        if apps:
            log.info("YouTube video %s: streams from YouTube's %s client", self.video_id, " + ".join(dict.fromkeys(apps)))
        for text in found[:resolve.MAX_WARNINGS] if isinstance(found, list) else []:
            if isinstance(text, str) and text.strip():
                log.warning("yt-dlp about %s: %s", self.video_id, privacy.clean(text)[:300])


class PlaylistJob(ResolveJob):
    """One run of resolve.py --playlist: found(Playlist) or failed(kind, message)."""

    def _parse_ok(self, raw):
        return streams.playlist_from_json(raw)


class CaptionJob(ResolveJob):
    """One run of resolve.py --captions: found(Path of the subtitle file) or failed(kind, message)."""

    def __init__(self, video_id: str, arguments: list, timeout_s: int, out: Path, parent=None):
        super().__init__(video_id, arguments, timeout_s, parent)
        self.out = out

    def _parse_ok(self, raw):
        path = Path(raw.get("file", "")) if isinstance(raw, dict) and isinstance(raw.get("file"), str) else None
        # only where it was asked to write (.vtt, or .srt when YouTube had no WebVTT)
        if path is None or path.parent != self.out.parent or path.name not in (self.out.name,
                                                                                self.out.with_suffix(".srt").name):
            raise streams.BadStream("no subtitle file where it was asked for")
        return path


class YouTube(QObject):
    """The window's YouTube part. ``opened(stream, start_s)`` when a video is ready to play."""
    opened = Signal(object, int)
    busy = Signal(str)             # "Finding …" text for the status line ("" = done)
    quietFailure = Signal(str, str)   # (video ID, problem code) of a video opened quietly (following the host)
    failed = Signal(str, str)         # (video ID, problem code) of any video that couldn't be opened
    playlistListed = Signal(object, object)    # (PlaylistRef, Playlist)

    def __init__(self, settings, cache_dir: Path, engine: Callable[[], str], parent=None):
        super().__init__(parent)
        self.settings = settings
        self.cache_dir = cache_dir
        self._playlist_jobs: set = set()
        self.engine = engine                     # "mpv" or "qt": Qt's player needs one stream with both
        self._cache: Dict[str, streams.Stream] = {}
        self._job: Optional[ResolveJob] = None
        self._job_start = 0
        self._job_quiet = False
        self._prefetch: Dict[str, ResolveJob] = {}
        self._failed_at: Dict[str, float] = {}   # video ID -> when it last failed (for "follow the host")

    # ------------------------------------------------------------ asking
    def check(self) -> Optional[str]:
        """None if YouTube can work here, else the problem code."""
        if not ytdlp_installed() or find_js_runtime(self.settings.get("youtube.js_runtime")) is None:
            return "SW-608"
        return None

    def open(self, ref: links.VideoRef, start_s: Optional[int] = None, quiet: bool = False) -> bool:
        """Find the video and emit opened(). quiet: problems only go to the log (following the host)."""
        start = ref.start_s if start_s is None else start_s
        problem = self.check()
        if problem:
            if not quiet:
                errors.report(problem, "yt-dlp or Deno not found")
            return False
        cached = self._cached(ref.id)
        if cached is not None:
            self._drop_job()
            self.opened.emit(cached, start)
            return True
        if self._job is not None and self._job.video_id == ref.id:
            self._job_start, self._job_quiet = start, quiet and self._job_quiet
            return True
        self._drop_job()
        waiting = self._prefetch.pop(ref.id, None)
        job = waiting or self._new_job(ref.id)
        self._job, self._job_start, self._job_quiet = job, start, quiet
        job.found.connect(self._found)
        job.failed.connect(self._failed)
        if waiting is None:
            job.start()
        self.busy.emit(f"Finding the YouTube video {ref.id}…")
        return True

    def ask_again(self, video_id: str, start_s: int, refused: str, tried: List[str], quiet: bool = False) -> bool:
        """YouTube refused the streams it gave its app ``refused`` ("TVHTML5", HTTP 403): ask yt-dlp
        again as other apps (streams.next_way). ``tried`` holds the ways asked already (one is added).
        False if there's no way left, or YouTube can't be asked here."""
        way = streams.next_way(refused, tried)
        if way is None or self.check():
            return False
        tried.append(way)
        log.info("YouTube refused the streams of %s (%s client): asking again as %s (try %d of %d)",
                 video_id, refused or "?", way, len(tried), streams.MAX_TRIES)
        self._cache.pop(video_id, None)            # (that stream is the one refused)
        self._drop_job()
        job = self._new_job(video_id, client=way)
        self._job, self._job_start, self._job_quiet = job, max(0, int(start_s)), quiet
        job.found.connect(self._found)
        job.failed.connect(self._failed)
        job.start()
        self.busy.emit(f"YouTube refused that stream, asking again another way ({len(tried)}/{streams.MAX_TRIES})…")
        return True

    def prefetch(self, video_id: str) -> None:
        """Look a video up ahead (the next one in the queue), so it starts at once later."""
        if not links.is_video_id(video_id) or self._cached(video_id) is not None or video_id in self._prefetch \
                or (self._job is not None and self._job.video_id == video_id) or self.check():
            return
        job = self._new_job(video_id)
        job.found.connect(self._remember)
        job.found.connect(lambda _stream: self._prefetch.pop(video_id, None))
        job.failed.connect(lambda _kind, _text: self._prefetch.pop(video_id, None))
        self._prefetch[video_id] = job
        job.start()

    def failed_recently(self, video_id: str, within_s: float = 60.0) -> bool:
        return time.monotonic() - self._failed_at.get(video_id, -1e9) < within_s

    def busy_with(self) -> str:
        return self._job.video_id if self._job is not None else ""

    def stop(self) -> None:
        self._drop_job()
        for job in list(self._prefetch.values()) + list(self._playlist_jobs):
            job.cancel()
        self._prefetch.clear()
        self._playlist_jobs.clear()

    # ------------------------------------------------------------ results
    def _new_job(self, video_id: str, client: str = "") -> ResolveJob:
        arguments = ["--height", str(self.settings.get("youtube.quality")),
                     "--codecs", str(self.settings.get("youtube.codecs")),
                     "--cache", str(self.cache_dir / "yt-dlp"),
                     "--timeout", str(max(10, int(self.settings.get("youtube.timeout_s")) - 5))]
        deno = find_js_runtime(self.settings.get("youtube.js_runtime"))
        if deno:
            arguments += ["--js", f"deno={deno}"]
        if self.engine() == "qt":
            arguments.append("--combined")
        if client:
            arguments += ["--client", client]
        return ResolveJob(video_id, arguments, int(self.settings.get("youtube.timeout_s")), self)

    def fetch_captions(self, video_id: str, lang: str, auto: bool, out: Path,
                       done: Callable[[Optional[Path], str], None]) -> Optional[CaptionJob]:
        """Get a video's subtitles in one language into ``out`` (resolve.py --captions);
        later done(file, "") or done(None, problem)."""
        if self.check():
            done(None, "yt-dlp or Deno not found")
            return None
        timeout = int(self.settings.get("youtube.timeout_s"))
        arguments = ["--captions", lang, "--out", str(out), "--cache", str(self.cache_dir / "yt-dlp"),
                     "--timeout", str(max(10, timeout - 5))] + (["--auto"] if auto else [])
        deno = find_js_runtime(self.settings.get("youtube.js_runtime"))
        if deno:
            arguments += ["--js", f"deno={deno}"]
        job = CaptionJob(video_id, arguments, timeout, out, self)
        job.found.connect(lambda path: (done(path, ""), job.deleteLater()))
        job.failed.connect(lambda kind, text: (done(None, f"{kind}: {text}"), job.deleteLater()))
        job.start()
        return job

    def list_playlist(self, ref: links.PlaylistRef) -> None:
        """Ask yt-dlp for a playlist's videos (IDs and titles only); playlistListed or failed comes."""
        arguments = ["--playlist", "--timeout", str(self.settings.get("youtube.timeout_s")),
                     "--cache", str(self.cache_dir / "yt-dlp")]
        job = PlaylistJob(ref.id, arguments, int(self.settings.get("youtube.timeout_s")) + 15, self)
        job.found.connect(lambda playlist, r=ref, j=job: (self._playlist_jobs.discard(j),
                                                         self.playlistListed.emit(r, playlist)))
        job.failed.connect(lambda kind, text, r=ref, j=job: (self._playlist_jobs.discard(j),
                                                            self._playlist_failed(r, kind, text)))
        self._playlist_jobs.add(job)
        self.busy.emit("Reading the playlist…")
        job.start()

    def _playlist_failed(self, ref: links.PlaylistRef, kind: str, text: str) -> None:
        log.info("playlist %s: %s (%s)", ref.id, kind, text[:300])
        self.busy.emit("")
        self.failed.emit(ref.id, PROBLEMS.get(kind, "SW-601"))

    def _cached(self, video_id: str) -> Optional[streams.Stream]:
        stream = self._cache.get(video_id)
        if stream is not None and stream.expired():
            del self._cache[video_id]
            return None
        return stream

    def _remember(self, stream: streams.Stream) -> None:
        self._cache[stream.id] = stream
        while len(self._cache) > CACHE_SIZE:
            self._cache.pop(next(iter(self._cache)))

    def _drop_job(self) -> None:
        if self._job is not None:
            self._job.cancel()
            self._job.deleteLater()
            self._job = None
            self.busy.emit("")

    def _found(self, stream: streams.Stream) -> None:
        job = self.sender()
        if job is not self._job:
            return
        took = time.monotonic() - job.started_at if job.started_at else 0.0
        log.info("found YouTube video %s in %.1f s: %s", stream.id, took, stream.describe())
        self._remember(stream)
        self._failed_at.pop(stream.id, None)
        start = self._job_start
        self._job = None
        job.deleteLater()
        self.busy.emit("")
        self.opened.emit(stream, start)

    def _failed(self, kind: str, message: str) -> None:
        job = self.sender()
        if job is not self._job:
            return
        self._failed_at[job.video_id] = time.monotonic()
        quiet = self._job_quiet
        self._job = None
        job.deleteLater()
        self.busy.emit("")
        code = PROBLEMS.get(kind, "SW-605")
        log.warning("YouTube video %s: %s (%s)", job.video_id, kind, message)
        self.failed.emit(job.video_id, code)
        if quiet:
            self.quietFailure.emit(job.video_id, code)
            return
        errors.report(code, f"{job.video_id}: {message}")


class TitleLookup(QObject):
    """A video's title from YouTube's oEmbed address (quick, no yt-dlp): for the queue, so people see
    what's coming. Each video is asked once; found(video ID, title) from a background thread."""
    found = Signal(str, str)
    ADDRESS = "https://www.youtube.com/oembed?format=json&url="

    def __init__(self, parent=None):
        super().__init__(parent)
        self._asked: set = set()

    def ask(self, video_id: str) -> None:
        if not links.is_video_id(video_id) or video_id in self._asked:
            return
        self._asked.add(video_id)
        threading.Thread(target=self._fetch, args=(video_id,), name="youtube-title", daemon=True).start()

    def _fetch(self, video_id: str) -> None:
        url = self.ADDRESS + quote(f"https://www.youtube.com/watch?v={video_id}", safe="")
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "SyncWatch"})
            with urllib.request.urlopen(request, timeout=8) as answer:
                data = json.loads(answer.read(200_000).decode("utf-8", "replace"))
            title = data.get("title") if isinstance(data, dict) else None
            if isinstance(title, str) and title.strip():
                self.found.emit(video_id, " ".join(title.split())[:120])
        except Exception as exc:  # noqa: BLE001 - only a nicer name for the queue
            log.debug("no title for %s: %s", video_id, exc)


class LinkBox(QWidget):
    """A field for a YouTube link, a Watch button and (optional) an Add to queue button.
    Emits submitted(VideoRef) (watch now) or queued(VideoRef). A magnet link or a web link (a film or a
    .torrent at an http(s) address) emits magnetSubmitted / webSubmitted with the text."""
    submitted = Signal(object)
    queued = Signal(object)
    magnetSubmitted = Signal(str)     # a magnet link (from 0.5: torrents)
    webSubmitted = Signal(str)        # a web link (from 0.5.1)
    playlistSubmitted = Signal(object, bool)      # (PlaylistRef, play: False = only into the queue)

    def __init__(self, button_text: str = "▶  Watch", parent=None):
        super().__init__(parent)
        self.field = QLineEdit()
        self.field.setPlaceholderText("Paste a YouTube, magnet or web link…")
        self.field.setClearButtonEnabled(True)
        self.field.returnPressed.connect(self._submit)
        self.field.textChanged.connect(self._changed)
        self.button = QPushButton(button_text)
        self.button.setObjectName("Accent")
        self.button.clicked.connect(self._submit)
        self.queue_button = QPushButton("➕  Queue")
        self.queue_button.setToolTip("Add it to the queue: it plays after the current video")
        self.queue_button.clicked.connect(self._queue)
        self.note = QLabel("")
        self.note.setObjectName("Hint")
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(self.field, 1)
        row.addWidget(self.button)
        row.addWidget(self.queue_button)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        layout.addLayout(row)
        layout.addWidget(self.note)
        self.note.hide()
        self._changed("")

    def _changed(self, text: str) -> None:
        ref = links.parse(text) or links.parse_playlist(text)
        magnet = torrent_links.is_magnet(text)
        web = ref is None and not magnet and web_links.parse(text) is not None
        self.button.setEnabled(ref is not None or magnet or web)
        self.queue_button.setEnabled(ref is not None)
        bad = bool(text.strip()) and ref is None and not magnet and not web
        self.note.setText("That isn't a YouTube, magnet or web link." if bad else "")
        self.note.setVisible(bad)

    def _submit(self) -> None:
        self._send(play=True)

    def _queue(self) -> None:
        self._send(play=False)

    def _send(self, play: bool) -> None:
        text = self.field.text().strip()
        playlist, ref = links.parse_playlist(text), links.parse(text)
        if playlist is None and ref is None:
            if play and torrent_links.is_magnet(text):     # a magnet link (0.5: torrents)
                self.field.clear()
                self.magnetSubmitted.emit(text)
            elif play and web_links.parse(text) is not None:   # a web link (0.5.1)
                self.field.clear()
                self.webSubmitted.emit(text)
            return
        self.field.clear()
        if playlist is not None:
            self.playlistSubmitted.emit(playlist, play)
        else:
            (self.submitted if play else self.queued).emit(ref)


class LinkDialog(QDialog):
    """Ctrl+L while a video plays: a small window with the link box."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Open a link")
        self.box = LinkBox()
        self.box.submitted.connect(lambda ref: self._chosen(ref, "watch"))
        self.box.queued.connect(lambda ref: self._chosen(ref, "queue"))
        self.box.playlistSubmitted.connect(lambda ref, play: self._chosen(ref, "playlist" if play else "playlist-queue"))
        self.box.magnetSubmitted.connect(lambda text: self._chosen_text(text, "magnet"))
        self.box.webSubmitted.connect(lambda text: self._chosen_text(text, "web"))
        self.ref: Optional[links.VideoRef] = None
        self.text = ""           # a magnet or web link (mode "magnet" / "web")
        self.mode = "watch"
        hint = QLabel("In a room, everyone who follows you opens the same video.")
        hint.setObjectName("Hint")
        layout = QVBoxLayout(self)
        layout.addWidget(self.box)
        layout.addWidget(hint)
        self.setMinimumWidth(460)
        self.box.field.setFocus(Qt.FocusReason.OtherFocusReason)

    def _chosen(self, ref: links.VideoRef, mode: str) -> None:
        self.ref, self.mode = ref, mode
        self.accept()

    def _chosen_text(self, text: str, mode: str) -> None:
        self.text, self.mode = text, mode
        self.accept()
