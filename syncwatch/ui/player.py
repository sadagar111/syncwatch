"""The video player: Qt's QMediaPlayer plus what sync needs from it.

What this adds on top of QMediaPlayer:
* A smooth position. Qt only updates the position about every 50 ms, so
  between updates it's estimated from the clock (never more than 300 ms ahead).
* "Ready": after a jump the player needs a moment to decode the new picture.
  Qt reports the new position instantly, but measured on a 1080p file the
  picture arrived about a second later, so "ready" waits for the first
  decoded picture at the new spot. Sync up waits for every PC to be ready
  before starting together.
* Clear errors for broken or unsupported files (SW-205 / SW-206 / SW-207).
* Sound follows your devices: if headphones are unplugged, playback moves to
  the Windows default device instead of going silent (SW-211).
* Audio track names and languages for the audio menu.
"""
from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import List, Optional, Tuple

from PySide6.QtCore import QLocale, QObject, QUrl, Signal
from PySide6.QtMultimedia import QAudioOutput, QMediaDevices, QMediaMetaData, QMediaPlayer

from .. import errors

log = logging.getLogger("syncwatch.player")

MAX_EXTRAPOLATE_MS = 300
SEEK_SETTLE_S = 1.5
FRAME_TOLERANCE_MS = 400     # a decoded picture this close to the jump target means "ready"
FRAME_WAIT_LIMIT_S = 4.0     # never wait longer than this for the picture after a jump
VIDEO_EXTENSIONS = (".mp4", ".mkv", ".avi", ".mov", ".webm", ".m4v", ".wmv", ".mpg", ".mpeg", ".ts", ".m2ts", ".ogv")

try:  # Qt >= 6.7
    from PySide6.QtMultimedia import QtAudio as _QtAudio

    def _perceived_to_linear(value: float) -> float:
        return _QtAudio.convertVolume(value, _QtAudio.VolumeScale.LogarithmicVolumeScale,
                                      _QtAudio.VolumeScale.LinearVolumeScale)
except ImportError:  # pragma: no cover - older Qt
    def _perceived_to_linear(value: float) -> float:
        return value ** 2


class Player(QObject):
    playingChanged = Signal(bool)
    loaded = Signal()          # a new film is ready (duration known)
    tracksChanged = Signal()
    ended = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.mp = QMediaPlayer(self)
        self.audio = QAudioOutput(self)
        self.mp.setAudioOutput(self.audio)
        self.devices = QMediaDevices(self)
        self.devices.audioOutputsChanged.connect(self._audio_devices_changed)
        self.path: Optional[Path] = None
        self._anchor_pos = 0.0
        self._anchor_time = time.monotonic()
        self._seek_target: Optional[float] = None
        self._seek_time = 0.0
        self._loaded_once = False
        self._error_reported = False
        self._awaiting_frame = False
        self._frame_target = 0.0
        self._follow_default = True
        self._volume = 0.8
        self.mp.positionChanged.connect(self._on_position)
        self.mp.playbackStateChanged.connect(self._on_state)
        self.mp.mediaStatusChanged.connect(self._on_status)
        self.mp.errorOccurred.connect(self._on_error)
        self.mp.tracksChanged.connect(self._on_tracks)

    def attach_sink(self, sink) -> None:
        """Watch the decoded pictures (from the video widget's QVideoSink) to know when a jump is done."""
        sink.videoFrameChanged.connect(self._on_frame)

    # ------------------------------------------------------------ what sync needs
    def position(self) -> float:
        # Measured with Qt 6.12: after a jump *while playing*, Qt's clock keeps running while
        # the picture decodes and then catches up, so the position must keep moving. After a
        # jump while paused, the film stays exactly at the target.
        if not self.is_playing() and (self._awaiting_frame or self._seek_target is not None):
            return self._frame_target if self._awaiting_frame else self._seek_target
        if not self.is_playing() or self.mp.mediaStatus() in (QMediaPlayer.MediaStatus.BufferingMedia,
                                                              QMediaPlayer.MediaStatus.StalledMedia):
            return self._anchor_pos
        ahead = (time.monotonic() - self._anchor_time) * 1000.0 * self.mp.playbackRate()
        return self._anchor_pos + min(max(0.0, ahead), MAX_EXTRAPOLATE_MS)

    def duration(self) -> int:
        return max(0, self.mp.duration())

    def is_playing(self) -> bool:
        return self.mp.playbackState() == QMediaPlayer.PlaybackState.PlayingState

    def has_media(self) -> bool:
        return self.path is not None and self.mp.mediaStatus() not in (
            QMediaPlayer.MediaStatus.NoMedia, QMediaPlayer.MediaStatus.InvalidMedia,
            QMediaPlayer.MediaStatus.LoadingMedia)

    def is_ready(self) -> bool:
        if not self.has_media():
            return False
        if self._seek_target is not None or self._awaiting_frame:
            return False
        return self.mp.mediaStatus() in (QMediaPlayer.MediaStatus.LoadedMedia,
                                         QMediaPlayer.MediaStatus.BufferedMedia,
                                         QMediaPlayer.MediaStatus.EndOfMedia)

    def play(self) -> None:
        if self.has_media():
            self.mp.play()

    def pause(self) -> None:
        if self.has_media():
            self.mp.pause()

    def seek(self, ms: float) -> None:
        if not self.has_media():
            return
        if self.mp.playbackState() == QMediaPlayer.PlaybackState.StoppedState:
            self.mp.pause()  # a stopped player forgets jumps; paused shows the frame
        ms = max(0, int(ms))
        self._seek_target = float(ms)
        self._seek_time = time.monotonic()
        self._anchor_pos, self._anchor_time = float(ms), time.monotonic()
        self._awaiting_frame = self.mp.hasVideo()
        self._frame_target = float(ms)
        self.mp.setPosition(ms)

    # ------------------------------------------------------------ loading
    def load(self, path: Path) -> bool:
        if not path.is_file():
            errors.report("SW-204", f"not found: {path}")
            return False
        try:
            with open(path, "rb") as handle:
                handle.read(1)
        except PermissionError as exc:
            errors.report("SW-207", f"{type(exc).__name__}: {exc}")
            return False
        except OSError as exc:
            errors.report("SW-205", f"{type(exc).__name__}: {exc}")
            return False
        self.path = path
        self._loaded_once = False
        self._error_reported = False
        self._awaiting_frame = False
        self._seek_target = None
        self._anchor_pos, self._anchor_time = 0.0, time.monotonic()
        self.mp.setSource(QUrl.fromLocalFile(str(path)))
        log.info("loading film %s (%d MB)", path.name, path.stat().st_size // 1_000_000)
        return True

    def unload(self) -> None:
        self.mp.stop()
        self.mp.setSource(QUrl())
        self.path = None

    # ------------------------------------------------------------ sound
    def set_volume(self, percent: int, muted: bool) -> None:
        self._volume = max(0.0, min(1.0, percent / 100.0))
        self.audio.setVolume(_perceived_to_linear(self._volume))
        self.audio.setMuted(muted)

    def has_audio_device(self) -> bool:
        return bool(QMediaDevices.audioOutputs())

    def _audio_devices_changed(self) -> None:
        outputs = QMediaDevices.audioOutputs()
        current = self.audio.device()
        still_there = any(device.id() == current.id() for device in outputs)
        if self._follow_default or not still_there:
            default = QMediaDevices.defaultAudioOutput()
            if not default.isNull() and default.id() != current.id():
                self.audio.setDevice(default)
                if not still_there:
                    errors.report("SW-211", f"switched to {default.description()}")
                log.info("sound now plays on %s", default.description())

    # ------------------------------------------------------------ tracks
    def audio_tracks(self) -> List[Tuple[int, str, str]]:
        """[(index, label, language code)]"""
        tracks = []
        for index, meta in enumerate(self.mp.audioTracks()):
            code, name = _language(meta)
            title = meta.stringValue(QMediaMetaData.Key.Title) or ""
            bits = [f"Track {index + 1}"]
            if name:
                bits.append(name)
            if title and title.lower() != (name or "").lower():
                bits.append(title)
            tracks.append((index, " · ".join(bits), code))
        return tracks

    def active_audio_track(self) -> int:
        return self.mp.activeAudioTrack()

    def set_audio_track(self, index: int) -> None:
        if 0 <= index < len(self.mp.audioTracks()):
            self.mp.setActiveAudioTrack(index)

    # ------------------------------------------------------------ Qt signals
    def _on_frame(self, frame) -> None:
        if not self._awaiting_frame or not frame.isValid():
            return
        start_us = frame.startTime()
        if start_us < 0 or abs(start_us / 1000.0 - self._frame_target) < FRAME_TOLERANCE_MS:
            self._awaiting_frame = False  # the picture at the new spot is on screen: ready

    def _on_position(self, ms: int) -> None:
        if self._seek_target is not None:
            settled = abs(ms - self._seek_target) < 1500
            timed_out = time.monotonic() - self._seek_time > SEEK_SETTLE_S
            if not settled and not timed_out:
                return  # a stale update from before the jump
            self._seek_target = None
        self._anchor_pos, self._anchor_time = float(ms), time.monotonic()

    def _on_state(self, state) -> None:
        # Restart the estimate from the real position, so pausing never "jumps back".
        if self._seek_target is None:
            self._anchor_pos = float(self.mp.position())
        self._anchor_time = time.monotonic()
        self.playingChanged.emit(state == QMediaPlayer.PlaybackState.PlayingState)

    def _on_status(self, status) -> None:
        if status == QMediaPlayer.MediaStatus.LoadedMedia and not self._loaded_once:
            self._loaded_once = True
            # Never let Qt draw subtitles itself: SyncWatch draws them (with your settings).
            self.mp.setActiveSubtitleTrack(-1)
            if self.mp.playbackState() == QMediaPlayer.PlaybackState.StoppedState:
                self.mp.pause()  # show the first picture instead of black
            self.loaded.emit()
        elif status == QMediaPlayer.MediaStatus.InvalidMedia and not self._error_reported:
            self._error_reported = True
            errors.report("SW-205", f"Qt reports invalid media: {self.mp.errorString() or '-'}")
        elif status == QMediaPlayer.MediaStatus.EndOfMedia:
            self.ended.emit()
        if self._seek_target is not None and status in (QMediaPlayer.MediaStatus.BufferedMedia,
                                                        QMediaPlayer.MediaStatus.LoadedMedia) \
                and time.monotonic() - self._seek_time > SEEK_SETTLE_S:
            self._seek_target = None

    def _on_tracks(self) -> None:
        self.mp.setActiveSubtitleTrack(-1)
        self.tracksChanged.emit()

    def _on_error(self, error, message: str) -> None:
        if error == QMediaPlayer.Error.NoError or self._error_reported:
            return
        self._error_reported = True
        technical = f"{getattr(error, 'name', error)}: {message}"
        if error == QMediaPlayer.Error.AccessDeniedError:
            errors.report("SW-207", technical)
        elif not self._loaded_once or error == QMediaPlayer.Error.FormatError:
            errors.report("SW-205", technical)
        else:
            errors.report("SW-206", technical)

    def tick(self) -> None:
        """Called by the UI timer: gives up waiting for a jump that never reported back."""
        waited = time.monotonic() - self._seek_time
        if self._awaiting_frame and waited > FRAME_WAIT_LIMIT_S:
            log.info("no picture %.1f s after a jump; continuing anyway", waited)
            self._awaiting_frame = False
            self._anchor_pos, self._anchor_time = float(self.mp.position()), time.monotonic()
            self._seek_target = None
        if self._seek_target is not None and not self._awaiting_frame and waited > SEEK_SETTLE_S * 2:
            self._anchor_pos = float(self.mp.position())
            self._anchor_time = time.monotonic()
            self._seek_target = None


def _language(meta) -> Tuple[str, str]:
    """(two-letter code, English name) of a track's language, or ('', '') if unknown."""
    value = meta.value(QMediaMetaData.Key.Language)
    try:
        if isinstance(value, QLocale.Language) and value not in (QLocale.Language.AnyLanguage, QLocale.Language.C):
            return QLocale.languageToCode(value), QLocale.languageToString(value)
    except (TypeError, ValueError):
        pass
    return "", ""
