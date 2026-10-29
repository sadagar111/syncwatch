"""Startup checks, done in stages so a problem is pinned down to one area.

    1. App files     Python and library versions, settings, data folder, design files
    2. Video         video components, formats, sound device, which video player is in use
                     (mpv, or Qt's as the fallback), and a real 2-second test video played
                     invisibly with that player (proves films will play)
    3. Network       your addresses (home network / Tailscale) and whether the
                     hosting port is free

Problems in stage 3 never stop you from watching on your own. Each result
carries an SW code, so the popup and "Copy report" explain it the same way.
"""
from __future__ import annotations

import logging
import platform
from dataclasses import dataclass
from typing import List

from PySide6.QtCore import QObject, QTimer, QUrl, Signal

from .. import MIN_PYSIDE6, errors, paths
from ..net.addresses import local_addresses

log = logging.getLogger("syncwatch.checks")

OK, WARN, FAIL = "ok", "warn", "fail"
SELFTEST_FILE = paths.ASSETS_DIR / "selftest" / "selftest.mp4"
SELFTEST_TIMEOUT_MS = 6000


@dataclass
class CheckResult:
    stage: str
    name: str
    status: str
    detail: str
    code: str = ""
    technical: str = ""

    def line(self) -> str:
        mark = {OK: "OK  ", WARN: "WARN", FAIL: "FAIL"}[self.status]
        code = f" [{self.code}]" if self.code else ""
        tech = f" ({self.technical})" if self.technical else ""
        return f"{mark} {self.stage} / {self.name}: {self.detail}{code}{tech}"


def app_checks(data_ok: bool, data_error: str, startup_problems: List[str]) -> List[CheckResult]:
    import PySide6
    results = [CheckResult("App files", "Python", OK, f"Python {platform.python_version()}")]
    version = tuple(int(p) for p in PySide6.__version__.split(".")[:2] if p.isdigit())
    if version < MIN_PYSIDE6:
        results.append(CheckResult("App files", "Video library", FAIL, f"PySide6 {PySide6.__version__} is too old",
                                   "SW-103"))
    else:
        results.append(CheckResult("App files", "Video library", OK, f"PySide6 {PySide6.__version__}"))
    if data_ok:
        results.append(CheckResult("App files", "Settings folder", OK, "can save settings"))
    else:
        results.append(CheckResult("App files", "Settings folder", WARN, "can't save settings", "SW-105", data_error))
    for code in startup_problems:
        problem = errors.problem(code)
        results.append(CheckResult("App files", "Files", WARN, problem.title, code))
    missing = [name for name in ("logo.png", "background.png", "default_avatar.png", "theme.json")
               if not (paths.ASSETS_DIR / name).is_file()]
    if missing:
        results.append(CheckResult("App files", "Design files", WARN, "missing: " + ", ".join(missing), "SW-111"))
    else:
        results.append(CheckResult("App files", "Design files", OK, "all present"))
    return results


def video_checks() -> List[CheckResult]:
    try:
        from PySide6.QtMultimedia import QMediaDevices, QMediaFormat
    except ImportError as exc:
        return [CheckResult("Video", "Video components", FAIL, "couldn't load", "SW-201", errors.describe_exception(exc))]
    results = []
    formats = QMediaFormat().supportedFileFormats(QMediaFormat.ConversionMode.Decode)
    if not formats:
        results.append(CheckResult("Video", "Formats", FAIL, "no playable formats reported", "SW-202"))
    else:
        results.append(CheckResult("Video", "Formats", OK, f"{len(formats)} container formats"))
    outputs = QMediaDevices.audioOutputs()
    if not outputs:
        results.append(CheckResult("Video", "Sound device", WARN, "none found", "SW-210"))
    else:
        default = QMediaDevices.defaultAudioOutput()
        results.append(CheckResult("Video", "Sound device", OK, default.description() or outputs[0].description()))
    return results


def player_check(player, fallback_reason: str = "") -> CheckResult:
    """Which video player plays films: mpv (normal), or Qt's (chosen, or because mpv failed)."""
    if player.engine == "mpv":
        return CheckResult("Video", "Video player", OK, player.describe())
    if fallback_reason:
        return CheckResult("Video", "Video player", WARN, "mpv couldn't start, using Qt's player", "SW-209",
                           fallback_reason)
    return CheckResult("Video", "Video player", OK, f"{player.describe()} (chosen in the settings)")


def network_checks(relay: str) -> List[CheckResult]:
    """Network: this PC's addresses, and whether a relay server is set up for rooms.

    Whether the relay can actually be reached shows up when you host or join (it takes a
    moment to find out, and startup shouldn't wait for it).
    """
    results = []
    addresses = local_addresses()
    if addresses:
        text = ", ".join(f"{ip} ({label})" for ip, label in addresses)
        results.append(CheckResult("Network", "Your addresses", OK, text))
    else:
        results.append(CheckResult("Network", "Your addresses", WARN, "no network found", "SW-311"))
    if relay:
        results.append(CheckResult("Network", "Relay server", OK, relay))
    else:
        results.append(CheckResult("Network", "Relay server", WARN, "not set up yet: rooms are off", "SW-401"))
    return results


class VideoSelfTest(QObject):
    """Plays the tiny test video invisibly and reports whether a picture came out.

    ``engine`` is the player in use: "qt" plays it with Qt's player into an invisible
    picture sink; "mpv" with a second, hidden mpv that decodes without showing anything
    (it doesn't touch the real player, which may already be playing a film).
    """
    finished = Signal(object)  # CheckResult

    def __init__(self, parent=None, engine: str = "qt"):
        super().__init__(parent)
        self._done = False
        self.engine = engine
        self.player = None
        self.core = None
        self._poll = None

    def start(self) -> None:
        if not SELFTEST_FILE.is_file():
            self._finish(CheckResult("Video", "Test video", WARN, "test file missing, skipped", "SW-111",
                                     "assets/selftest/selftest.mp4"))
            return
        if self.engine == "mpv":
            self._start_mpv()
            return
        try:
            from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer, QVideoSink
        except ImportError as exc:
            self._finish(CheckResult("Video", "Test video", FAIL, "video components missing", "SW-201",
                                     errors.describe_exception(exc)))
            return
        self.player = QMediaPlayer(self)
        self.audio = QAudioOutput(self)
        self.audio.setMuted(True)
        self.player.setAudioOutput(self.audio)
        self.sink = QVideoSink(self)
        self.player.setVideoSink(self.sink)
        self.sink.videoFrameChanged.connect(self._frame)
        self.player.errorOccurred.connect(lambda error, text: self._finish(
            CheckResult("Video", "Test video", FAIL, "test video didn't play", "SW-203", f"{error.name}: {text}")))
        QTimer.singleShot(SELFTEST_TIMEOUT_MS, lambda: self._finish(
            CheckResult("Video", "Test video", FAIL, "no picture within 6 seconds", "SW-203", "timeout")))
        self.player.setSource(QUrl.fromLocalFile(str(SELFTEST_FILE)))
        self.player.play()

    def _frame(self, frame) -> None:
        if frame.isValid():
            self._finish(CheckResult("Video", "Test video", OK, "played fine"))

    def _start_mpv(self) -> None:
        from .. import mpvlib
        try:
            module = mpvlib.load()
            self.core = module.MPV(vo="null", ao="null", hwdec="no", idle="yes", keep_open="yes", config="no",
                                   load_scripts="no", ytdl="no", osc="no", input_default_bindings="no")
            self.core.loadfile(str(SELFTEST_FILE))
            self.core.pause = False
        except Exception as exc:  # noqa: BLE001 - any failure here means films won't play either
            self._finish(CheckResult("Video", "Test video", FAIL, "test video didn't play", "SW-203",
                                     errors.describe_exception(exc)))
            return
        self._poll = QTimer(self)
        self._poll.timeout.connect(self._check_mpv)
        self._poll.start(100)
        QTimer.singleShot(SELFTEST_TIMEOUT_MS, lambda: self._finish(
            CheckResult("Video", "Test video", FAIL, "nothing played within 6 seconds", "SW-203", "timeout (mpv)")))

    def _check_mpv(self) -> None:
        try:
            position = self.core.time_pos if self.core is not None else None
        except Exception:  # noqa: BLE001 - not available yet
            position = None
        if position is not None and position >= 0.3:
            self._finish(CheckResult("Video", "Test video", OK, "played fine"))

    def _finish(self, result: CheckResult) -> None:
        if self._done:
            return
        self._done = True
        if self.player is not None:
            self.player.stop()
            self.player.setSource(QUrl())
        if self._poll is not None:
            self._poll.stop()
        if self.core is not None:
            core, self.core = self.core, None
            try:
                core.terminate()
            except Exception:  # noqa: BLE001 - it's only the test player
                log.exception("stopping the test player")
        log.info("video self-test: %s", result.line())
        self.finished.emit(result)
