"""Which subtitles are available for the open film, which one is on, and its timing.

Sources are found automatically: subtitle files next to the film (same name,
optionally with a language tag, or in a Subs folder) and text tracks inside
.mkv/.webm films. A YouTube video's subtitles come from YouTube: fetched when
picked (by yt-dlp, in its own process) and kept in the cache for a while.
Your choice, delay and stretch are remembered per film (and per video).

Slow work (reading a film's subtitle tracks) runs in the background and shows
progress with an estimate of the time left; results are cached so each film
is only read once.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from PySide6.QtCore import QObject, Signal

from .. import errors
from ..errors import SyncWatchError
from ..filmstate import FilmMemory
from ..subtitles import mkv, parsers
from ..subtitles.model import SubtitleTrack

log = logging.getLogger("syncwatch.subtitles")

MKV_SUFFIXES = (".mkv", ".webm", ".mka", ".mk3d")
YOUTUBE_KEEP_S = 7 * 86400        # YouTube subtitles in the cache are used again for a week
YOUTUBE_TIDY_S = 30 * 86400       # and deleted after a month


@dataclass
class Source:
    id: str          # "file:<path>" | "mkv:<track number>" | "yt:<lang>" | "ytauto:<lang>" (YouTube's)
    label: str
    language: str
    usable: bool = True


class Subtitles(QObject):
    sourcesChanged = Signal()
    statusChanged = Signal(str)
    failed = Signal(str, str)     # (problem code, details) the window shows without a popup
    _bridge = Signal(object)      # runs a function on the GUI thread

    def __init__(self, settings, films: FilmMemory, cache_dir: Path, parent=None):
        super().__init__(parent)
        self.settings = settings
        self.films = films
        self.cache_dir = cache_dir
        self.sources: List[Source] = []
        self.current_id = "off"
        self.track: Optional[SubtitleTrack] = None
        self.delay_ms = 0
        self.stretch = 1.0
        self.status = ""
        self._film: Optional[Path] = None
        self._film_key = ""
        self._generation = 0
        self._mkv_tracks: Dict[int, SubtitleTrack] = {}
        self._mkv_infos: List[mkv.TrackInfo] = []
        self._mkv_state = "none"      # none | listing | ready | reading | done | failed
        self._cancel = threading.Event()
        self._bridge.connect(lambda fn: fn())
        self.video_id = ""            # the YouTube video that's open (its subtitles are YouTube's)
        # set by the window: YouTube.fetch_captions(video_id, lang, auto, out, done(file or None, problem))
        self.fetch_youtube = None

    # ------------------------------------------------------------ film changes
    def film_opened(self, film: Path, film_key: str) -> None:
        self._cancel.set()
        self._cancel = threading.Event()
        self._generation += 1
        self._film, self._film_key, self.video_id = film, film_key, ""
        self.sources, self._mkv_tracks, self._mkv_infos = [], {}, []
        self.track, self.current_id, self.status = None, "off", ""
        memory = self.films.get(film_key)
        self.delay_ms = int(memory.get("sub_delay_ms", 0))
        self.stretch = float(memory.get("sub_stretch", 1.0)) or 1.0
        for path, language in parsers.find_external(film):
            label = path.name if not language else f"{parsers.LANGUAGE_NAMES.get(language, language)} · {path.name}"
            self.sources.append(Source(f"file:{path}", label, language))
        self._mkv_state = "none"
        if film.suffix.lower() in MKV_SUFFIXES:
            self._mkv_state = "listing"
            generation = self._generation
            threading.Thread(target=self._list_mkv, args=(film, generation), daemon=True,
                             name="sw-subs-list").start()
        else:
            self._choose_initial(memory)
        self.sourcesChanged.emit()

    def stream_opened(self, film_key: str, video_id: str, captions) -> None:
        """A YouTube video: the uploader's subtitles, and YouTube's automatic ones in the spoken language
        and in your languages (it translates them into ~150). Fetched when picked."""
        self._cancel.set()
        self._cancel = threading.Event()
        self._generation += 1
        self._film, self._film_key, self.video_id = None, film_key, video_id
        self.sources, self._mkv_tracks, self._mkv_infos = [], {}, []
        self.track, self.current_id, self.status = None, "off", ""
        self._mkv_state = "none"
        memory = self.films.get(film_key)
        self.delay_ms = int(memory.get("sub_delay_ms", 0))
        self.stretch = float(memory.get("sub_stretch", 1.0)) or 1.0
        preferred = {parsers.normalise_language(p) for p in self.settings.languages("subs.languages")}
        spoken = {parsers.normalise_language(c.language) for c in captions if c.original}
        for caption in captions:
            language = parsers.normalise_language(caption.language)
            name = caption.name or parsers.LANGUAGE_NAMES.get(language, caption.language)
            if not caption.auto:
                self.sources.append(Source(f"yt:{caption.lang}", f"YouTube · {name}", language))
            elif caption.original or (language in preferred and language not in spoken):
                self.sources.append(Source(f"ytauto:{caption.lang}", f"YouTube, automatic · {name}", language))
        self._choose_initial(memory)
        self.sourcesChanged.emit()

    def _list_mkv(self, film: Path, generation: int) -> None:
        try:
            info = mkv.read_info(film)
            tracks, problem = info.tracks, None
        except mkv.NotMatroska as exc:
            tracks, problem = [], None
            log.info("not a Matroska file (%s): %s", film.name, exc)
        except (OSError, SyncWatchError, ValueError, EOFError) as exc:
            tracks, problem = [], exc
        self._bridge.emit(lambda: self._mkv_listed(generation, tracks, problem))

    def _mkv_listed(self, generation: int, tracks: List[mkv.TrackInfo], problem) -> None:
        if generation != self._generation:
            return
        if problem is not None:
            errors.report("SW-252", errors.describe_exception(problem))
        self._mkv_infos = tracks
        for info in tracks:
            self.sources.append(Source(f"mkv:{info.number}", f"In the film · {info.describe()}",
                                       info.language, usable=info.is_text))
        self._mkv_state = "ready"
        self._choose_initial(self.films.get(self._film_key))
        self.sourcesChanged.emit()

    def _choose_initial(self, memory: dict) -> None:
        remembered = memory.get("subs")
        if remembered:
            if remembered == "off" or any(s.id == remembered for s in self.sources):
                self.select(remembered, remember=False)
                return
        if not self.settings.get("subs.auto_load"):
            return
        preferred = self.settings.languages("subs.languages")
        usable = [s for s in self.sources if s.usable]
        files = [(s, s.language) for s in usable if s.id.startswith("file:")]
        tracks = [(s, s.language) for s in usable if s.id.startswith("mkv:")]
        # Your language order decides first; between a file and a track in the same
        # language, the file wins (it was put there on purpose).
        best = parsers.pick_preferred(files + tracks, preferred)
        if best is None and self.video_id:
            # the uploader's subtitles in any of your languages before YouTube's automatic ones
            best = parsers.pick_preferred([(s, s.language) for s in usable if s.id.startswith("yt:")], preferred)
            if best is None and self.settings.get("youtube.auto_captions"):
                best = parsers.pick_preferred([(s, s.language) for s in usable if s.id.startswith("ytauto:")],
                                              preferred)
        if best is None and len(files) == 1 and not files[0][1]:
            best = files[0][0]  # a single file without a language tag: probably the right one
        if best is not None:
            self.select(best.id, remember=False)

    # ------------------------------------------------------------ choosing
    def select(self, source_id: str, remember: bool = True) -> None:
        self.current_id = source_id
        self.track = None
        if remember and self._film_key:
            self.films.update(self._film_key, subs=source_id)
        if source_id == "off":
            self._set_status("")
        elif source_id.startswith("file:"):
            self._load_file(Path(source_id[5:]))
        elif source_id.startswith("mkv:"):
            self._load_mkv_track(int(source_id[4:]))
        elif source_id.startswith(("yt:", "ytauto:")):
            self._load_youtube(source_id)
        self.sourcesChanged.emit()

    def _load_file(self, path: Path, source_id: str = "") -> None:
        generation = self._generation
        source_id = source_id or f"file:{path}"
        stem = self._film.stem if self._film else ""
        encoding = self.settings.get("subs.encoding")
        preferred = self.settings.languages("subs.languages")

        def work() -> None:
            try:
                track = parsers.load_file(path, encoding, preferred, stem)
                outcome = (track, None)
            except SyncWatchError as exc:
                outcome = (None, exc)
            self._bridge.emit(lambda: self._file_loaded(generation, source_id, *outcome))

        self._set_status("Loading subtitles…")
        threading.Thread(target=work, daemon=True, name="sw-subs-file").start()

    def _file_loaded(self, generation: int, source_id: str, track: Optional[SubtitleTrack],
                     problem: Optional[SyncWatchError]) -> None:
        if generation != self._generation or self.current_id != source_id:
            return
        if problem is not None:
            errors.report(problem.code, problem.technical, **problem.params)
            self.current_id = "off"
            self._set_status("")
            self.sourcesChanged.emit()
            return
        self.track = track
        encoding = getattr(track, "encoding", "")
        self._set_status(f"{len(track)} subtitles loaded" + (f" ({encoding})" if encoding not in ("utf-8", "") else ""))

    # ------------------------------------------------------------ YouTube's subtitles
    def _youtube_file(self, source_id: str) -> Path:
        kind, _, lang = source_id.partition(":")
        return self.cache_dir / "youtube-subtitles" / f"{self.video_id}.{lang}{'.auto' if kind == 'ytauto' else ''}.vtt"

    def _load_youtube(self, source_id: str) -> None:
        out = self._youtube_file(source_id)
        for path in (out, out.with_suffix(".srt")):     # (fetched before, recently)
            try:
                if time.time() - path.stat().st_mtime < YOUTUBE_KEEP_S:
                    self._load_file(path, source_id)
                    return
            except OSError:
                pass
        if self.fetch_youtube is None or not self.video_id:
            self.current_id = "off"
            return
        generation = self._generation
        try:
            out.parent.mkdir(parents=True, exist_ok=True)
            self._tidy_youtube(out.parent)
        except OSError as exc:
            log.warning("no folder for YouTube subtitles: %s", exc)
        self._set_status("Getting the subtitles from YouTube…")
        self.fetch_youtube(self.video_id, source_id.partition(":")[2], source_id.startswith("ytauto:"), out,
                           lambda path, problem: self._youtube_fetched(generation, source_id, path, problem))

    def _youtube_fetched(self, generation: int, source_id: str, path: Optional[Path], problem: str) -> None:
        if generation != self._generation or self.current_id != source_id:
            return
        if path is None:
            self.failed.emit("SW-611", problem[:2000])
            self.current_id = "off"
            self._set_status("")
            self.sourcesChanged.emit()
            return
        self._load_file(path, source_id)

    @staticmethod
    def _tidy_youtube(folder: Path) -> None:
        """Subtitles fetched more than a month ago go (a few hundred KB each)."""
        now = time.time()
        for path in folder.iterdir():
            try:
                if path.is_file() and now - path.stat().st_mtime > YOUTUBE_TIDY_S:
                    path.unlink()
            except OSError:
                pass

    def _load_mkv_track(self, number: int) -> None:
        info = next((t for t in self._mkv_infos if t.number == number), None)
        if info is None:
            return
        if not info.is_text:
            errors.report("SW-253", f"track {number} codec {info.codec}")
            self.current_id = "off"
            return
        if number in self._mkv_tracks:
            self.track = self._mkv_tracks[number]
            return
        cached = mkv.load_cache(self.cache_dir, self._film) if self._film else None
        text_tracks = [t for t in self._mkv_infos if t.is_text]
        if cached is not None:
            for t in text_tracks:
                self._mkv_tracks[t.number] = SubtitleTrack(cached.get(t.number, []), label=t.describe(),
                                                           language=t.language, source=f"mkv:{t.number}")
            self.track = self._mkv_tracks.get(number)
            self._set_status("Subtitles loaded (from cache)")
            return
        for t in text_tracks:
            track = SubtitleTrack(label=t.describe(), language=t.language, source=f"mkv:{t.number}")
            track.complete = False
            self._mkv_tracks[t.number] = track
        self.track = self._mkv_tracks[number]
        self._start_mkv_scan(text_tracks)

    def _start_mkv_scan(self, text_tracks: List[mkv.TrackInfo]) -> None:
        if self._mkv_state == "reading" or self._film is None:
            return
        self._mkv_state = "reading"
        film, generation, cancel = self._film, self._generation, self._cancel
        limit = float(self.settings.get("subs.mkv_read_limit_mb"))
        tracks = dict(self._mkv_tracks)

        def deliver(number: int, cues) -> None:
            if number in tracks:
                tracks[number].add(cues)

        def progress(fraction: float, eta: Optional[float]) -> None:
            left = f", about {_eta(eta)} left" if eta else ""
            self._bridge.emit(lambda: self._scan_progress(generation, f"Reading subtitles from the film… {fraction:.0%}{left}"))

        def work() -> None:
            problem = None
            try:
                finished = mkv.extract(film, text_tracks, deliver, progress, cancel, limit)
            except SyncWatchError as exc:
                finished, problem = False, exc
            self._bridge.emit(lambda: self._scan_done(generation, tracks, finished, problem))

        threading.Thread(target=work, daemon=True, name="sw-subs-mkv").start()

    def _scan_progress(self, generation: int, text: str) -> None:
        if generation == self._generation:
            self._set_status(text)

    def _scan_done(self, generation: int, tracks: Dict[int, SubtitleTrack], finished: bool, problem) -> None:
        if generation != self._generation:
            return
        for track in tracks.values():
            track.complete = True
        if problem is not None:
            self._mkv_state = "failed"
            errors.report(problem.code, problem.technical, **problem.params)
            self._set_status("Some subtitles couldn't be read")
            return
        self._mkv_state = "done" if finished else "ready"
        if finished and self._film is not None:
            mkv.save_cache(self.cache_dir, self._film, {n: t.cues() for n, t in tracks.items()})
            self._set_status("Subtitles loaded")

    # ------------------------------------------------------------ timing
    def nudge(self, delta_ms: int) -> None:
        self.set_delay(self.delay_ms + delta_ms)

    def set_delay(self, delay_ms: int) -> None:
        self.delay_ms = int(max(-36_000_000, min(36_000_000, delay_ms)))
        if self._film_key:
            self.films.update(self._film_key, sub_delay_ms=self.delay_ms)
        self.sourcesChanged.emit()

    def set_stretch(self, factor: float) -> None:
        self.stretch = factor if 0.5 <= factor <= 2.0 else 1.0
        if self._film_key:
            self.films.update(self._film_key, sub_stretch=self.stretch)
        self.sourcesChanged.emit()

    def cues_at(self, position_ms: float) -> List[Tuple[str, bool]]:
        if self.track is None:
            return []
        return [(c.text, c.top) for c in self.track.active(position_ms, self.delay_ms, self.stretch)]

    def load_external(self, path: Path) -> None:
        """A file picked by hand (Subtitles > Load file)."""
        source_id = f"file:{path}"
        if not any(s.id == source_id for s in self.sources):
            language = parsers.language_from_name(path, self._film.stem if self._film else "")
            self.sources.insert(0, Source(source_id, path.name, language))
        self.select(source_id)

    def stop(self) -> None:
        self._cancel.set()

    def film_closed(self) -> None:
        """No film anymore (it's being removed from the library): stop reading it, show nothing."""
        self._cancel.set()
        self._generation += 1
        self._film, self._film_key, self.video_id = None, "", ""
        self.sources, self._mkv_tracks, self._mkv_infos = [], {}, []
        self.track, self.current_id, self.status = None, "off", ""
        self._mkv_state = "none"
        self.sourcesChanged.emit()

    def _set_status(self, text: str) -> None:
        if text != self.status:
            self.status = text
            self.statusChanged.emit(text)


def _eta(seconds: float) -> str:
    seconds = int(seconds + 0.5)
    return f"{seconds} s" if seconds < 90 else f"{seconds // 60} min {seconds % 60:02d} s"
