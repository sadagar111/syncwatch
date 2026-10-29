"""Choosing the video player: mpv (from 0.4.2) or Qt's (the fallback).

mpv plays films by default (mpv_player.py). Qt's player (player.py), SyncWatch's only
player up to 0.4.1, stays as the fallback: it's used when Settings > Playback >
"Video player" (advanced) is set to Qt, and by itself when mpv can't start on this PC.
That's reported as SW-209 and shows up in the startup checks.

The window asks for a player with ``create()``, puts ``player.video_widget()`` where the
picture goes and then calls ``start_or_fallback()``.
"""
from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QObject, QTimer, QUrl, Signal
from PySide6.QtGui import QGuiApplication

from .. import errors, mpvlib
from .mpv_player import MpvPlayer, StartFailed, choose_video_output
from .player import Player as QtPlayer

log = logging.getLogger("syncwatch.player")

# Why mpv isn't being used, if it isn't although it was chosen (for the startup checks).
fallback_reason: Optional[str] = None


def create(settings, parent=None):
    """The player chosen in the settings, or the Qt player if mpv can't be loaded."""
    global fallback_reason
    fallback_reason = None
    if settings.get("playback.engine") == "mpv":
        try:
            module = mpvlib.load()
        except mpvlib.Unavailable as exc:
            _fall_back(str(exc))
        else:
            output = choose_video_output(settings.get("playback.video_drawing"), QGuiApplication.platformName())
            return MpvPlayer(module, output, settings.get("playback.hwdec"), parent)
    return QtPlayer(parent)


def start_or_fallback(player, make_widget, parent=None):
    """Start ``player``. If mpv fails to start, return a started Qt player instead.

    ``make_widget(player)`` puts the player's video widget into the window (and removes
    the previous one); it's called again for the replacement. Returns the player in use.
    """
    try:
        player.start()
        return player
    except StartFailed as exc:
        _fall_back(str(exc))
        player.shutdown()
        replacement = QtPlayer(parent)
        make_widget(replacement)
        replacement.start()
        return replacement


def _fall_back(reason: str) -> None:
    global fallback_reason
    fallback_reason = reason
    errors.report("SW-209", reason)


class PlayabilityTest(QObject):
    """Plays a film invisibly for a moment with the player in use: do its picture and sound work?

    Used by Library > Add film. ``finished`` carries a dict:
    {"picture": bool, "sound": bool or None (no sound track), "detail": text for the details}.
    It never touches the real player, which may be playing a film right now.
    """
    finished = Signal(object)
    PLAY_S = 0.6        # how much of the film must play
    LIMIT_S = 10.0      # give up after this long

    def __init__(self, engine: str, path: Path, parent=None):
        super().__init__(parent)
        self.engine = engine
        self.path = path
        self._done = False
        self._started = 0.0
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._check)
        self.core = None
        self._mpv_problems: list = []
        self.mp = None
        self._frames = 0
        self._qt_error = ""

    def start(self) -> None:
        self._started = time.monotonic()
        try:
            if self.engine == "mpv":
                self._start_mpv()
            else:
                self._start_qt()
        except Exception as exc:  # noqa: BLE001 - the test itself failing means: can't tell, so say no
            self._finish(False, None, f"test failed to start: {errors.describe_exception(exc)}")
            return
        self._timer.start(100)

    # ---- mpv: a second, hidden mpv that decodes without showing or playing anything
    def _start_mpv(self) -> None:
        module = mpvlib.load()

        def collect(level, prefix, text):   # mpv's thread: list.append is safe
            if "Failed to initialize a decoder" in text or "Could not initialize video chain" in text:
                self._mpv_problems.append((prefix, text.strip()))

        self.core = module.MPV(vo="null", ao="null", hwdec="no", idle="yes", keep_open="yes", config="no",
                               load_scripts="no", ytdl="no", osc="no", sid="no", input_default_bindings="no",
                               log_handler=collect, loglevel="error")
        self.core.loadfile(str(self.path))
        self.core.pause = False

    def _mpv_value(self, name):
        try:   # (python-mpv: attributes are mpv's properties; core["x"] would be the option "x")
            return getattr(self.core, name.replace("-", "_"))
        except Exception:  # noqa: BLE001 - not available (yet)
            return None

    def _check_mpv(self, waited: float) -> None:
        position = self._mpv_value("time-pos")
        failed_sound = any(prefix == "ad" for prefix, _ in self._mpv_problems)
        failed_picture = any(prefix != "ad" for prefix, _ in self._mpv_problems)
        detail = "; ".join(text for _, text in self._mpv_problems)
        if failed_picture:
            self._finish(False, None, detail)
        elif (position is not None and position >= self.PLAY_S) or waited > self.LIMIT_S:
            tracks = self._mpv_value("track-list") or []
            has_sound = any(isinstance(t, dict) and t.get("type") == "audio" for t in tracks)
            picture = bool(self._mpv_value("video-codec")) and position is not None
            sound = (not failed_sound and bool(self._mpv_value("audio-codec"))) if has_sound else None
            self._finish(picture, sound, detail or ("timeout" if waited > self.LIMIT_S else ""))

    # ---- Qt: QMediaPlayer into an invisible picture sink, muted
    def _start_qt(self) -> None:
        from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer, QVideoSink
        self.mp = QMediaPlayer(self)
        self.audio = QAudioOutput(self)
        self.audio.setMuted(True)
        self.mp.setAudioOutput(self.audio)
        self.sink = QVideoSink(self)
        self.mp.setVideoSink(self.sink)
        self.sink.videoFrameChanged.connect(lambda frame: setattr(self, "_frames", self._frames + 1))
        self.mp.errorOccurred.connect(lambda error, text: setattr(self, "_qt_error", f"{error.name}: {text}"))
        self.mp.setSource(QUrl.fromLocalFile(str(self.path)))
        self.mp.play()

    def _check_qt(self, waited: float) -> None:
        if self._qt_error:
            self._finish(self._frames > 0, False if self._frames > 0 else None, self._qt_error)
        elif self.mp.position() >= self.PLAY_S * 1000 or waited > self.LIMIT_S:
            sound = self.mp.hasAudio() if self.mp.audioTracks() else None
            self._finish(self._frames > 0, sound, "timeout" if waited > self.LIMIT_S else "")

    # ---- both
    def _check(self) -> None:
        waited = time.monotonic() - self._started
        if self.engine == "mpv":
            self._check_mpv(waited)
        else:
            self._check_qt(waited)

    def _finish(self, picture: bool, sound, detail: str) -> None:
        if self._done:
            return
        self._done = True
        self._timer.stop()
        if self.core is not None:
            core, self.core = self.core, None
            try:
                core.terminate()
            except Exception:  # noqa: BLE001 - only the test player
                log.exception("stopping the test player")
        if self.mp is not None:
            self.mp.stop()
            self.mp.setSource(QUrl())
        result = {"picture": picture, "sound": sound, "detail": detail}
        log.info("playability test (%s) of %s: %s", self.engine, self.path.name, result)
        self.finished.emit(result)
