"""The mpv video player (the default from 0.4.2): libmpv inside SyncWatch's window.

It has the same interface as the Qt player (player.py), so the sync controller and the
window don't care which one plays. Why mpv: it plays practically every film (including
the AC3 / DTS sound Qt's player sometimes couldn't), it's a mature player, and
v0.5.0's YouTube streaming builds on it. It comes inside the zip (see mpvlib.py).

How the picture gets into the window (Settings > Playback > "How mpv draws the picture"):

* window: mpv draws into its own child window in SyncWatch's video area, with Direct3D
  on Windows. Fastest, and the overlay (subtitles, messages) sits on top like with the
  Qt player. On Windows that child window is disabled (winfix.py), so clicks,
  double-clicks and the mouse wheel reach SyncWatch instead of mpv.
* opengl: SyncWatch's own OpenGL widget asks mpv for each picture (mpv's "render API").
  Works wherever OpenGL works: the thing to try if the picture stays black.
* none: no picture at all. Only for the automatic tests on machines without a screen.

Threads: libmpv reports through python-mpv's event thread. Everything arriving there is
handed to the GUI thread with a Qt signal (stamped with the time it arrived), so all the
state below is only ever touched by the GUI thread.

Position: mpv reports the position with every video frame. Between reports it's
estimated from the clock (at most 250 ms ahead), like the Qt player does. After a jump,
mpv waits until the picture at the new spot is decoded and only then continues
(measured: 0.3 to 1 s on a 1080p film with keyframes 10 s apart). Until then the
position stays at the jump target and the player isn't "ready".
"""
from __future__ import annotations

import logging
import re
import sys
import time
from pathlib import Path
from typing import List, Optional, Tuple

from PySide6.QtCore import QByteArray, QLocale, QObject, Qt, Signal
from PySide6.QtGui import QColor, QOpenGLContext, QPalette
from PySide6.QtOpenGLWidgets import QOpenGLWidget
from PySide6.QtWidgets import QWidget

from .. import errors, mpvlib
from . import winfix

log = logging.getLogger("syncwatch.mpv")
core_log = logging.getLogger("syncwatch.mpv.core")

MAX_EXTRAPOLATE_MS = 250
WAIT_GIVE_UP_S = 8.0          # a load or jump that never reports back counts as done after this
CHILD_WINDOW_CHECK_S = 1.0    # how often to make sure mpv's child window ignores the mouse (Windows)
GL_FRAMEBUFFER = 0x8D40
CHANNELS = {1: "mono", 2: "stereo", 6: "5.1", 8: "7.1"}
VIDEO_OUTPUTS = ("window", "opengl", "none")


class StartFailed(Exception):
    """mpv couldn't be started; the window falls back to the Qt player (SW-209)."""


def choose_video_output(setting: str, platform_name: str) -> str:
    """'window' / 'opengl' / 'none' from the setting and the Qt platform."""
    if platform_name in ("offscreen", "minimal"):
        return "none"          # automatic tests: there's no screen to draw on
    if setting in ("window", "opengl"):
        return setting
    return "window" if sys.platform == "win32" else "opengl"


def language(code: str) -> Tuple[str, str]:
    """(two-letter code, English name) of an mpv track language such as 'slo' or 'eng'."""
    if not code:
        return "", ""
    try:
        lang = QLocale.codeToLanguage(code.strip())
        if lang not in (QLocale.Language.AnyLanguage, QLocale.Language.C):
            return QLocale.languageToCode(lang, QLocale.LanguageCodeType.ISO639Part1), QLocale.languageToString(lang)
    except (TypeError, ValueError, AttributeError):
        pass
    return "", ""


class NativeVideo(QWidget):
    """The video area for 'window' (mpv draws into it) and 'none' (stays empty) modes."""

    def __init__(self, background: str, native: bool, parent=None):
        super().__init__(parent)
        if native:
            self.setAttribute(Qt.WidgetAttribute.WA_NativeWindow)
            self.setAttribute(Qt.WidgetAttribute.WA_DontCreateNativeAncestors)
        palette = self.palette()
        palette.setColor(QPalette.ColorRole.Window, QColor(background))
        self.setPalette(palette)
        self.setAutoFillBackground(True)


class GLVideo(QOpenGLWidget):
    """The video area for 'opengl' mode: mpv renders each picture into this widget."""
    _frame_ready = Signal()   # from mpv's render thread: a new picture can be drawn

    def __init__(self, player: "MpvPlayer", background: str, parent=None):
        super().__init__(parent)
        self.player = player
        self.background = QColor(background)
        self.render_context = None
        self.failed = False
        self._get_proc_address = None
        self._frame_ready.connect(self.update, Qt.ConnectionType.QueuedConnection)
        self.frameSwapped.connect(self._swapped)

    def _create_context(self) -> None:
        core = self.player.core
        if core is None or self.render_context is not None or self.failed:
            return
        module = self.player.module

        def get_proc_address(_ctx, name):
            context = QOpenGLContext.currentContext()
            return int(context.getProcAddress(QByteArray(name)) or 0) if context is not None else 0

        self._get_proc_address = module.MpvGlGetProcAddressFn(get_proc_address)
        try:
            self.render_context = module.MpvRenderContext(
                core, "opengl", opengl_init_params={"get_proc_address": self._get_proc_address})
        except Exception as exc:  # noqa: BLE001 - any failure means: no picture this way
            self.failed = True
            errors.report("SW-208", f"OpenGL render context: {errors.describe_exception(exc)}")
            return
        self.render_context.update_cb = self._frame_ready.emit

    def initializeGL(self) -> None:  # noqa: N802
        self._create_context()

    def paintGL(self) -> None:  # noqa: N802
        if self.render_context is None:
            self._create_context()
        functions = QOpenGLContext.currentContext().functions()
        if self.render_context is None:
            functions.glClearColor(self.background.redF(), self.background.greenF(), self.background.blueF(), 1.0)
            functions.glClear(0x4000)  # GL_COLOR_BUFFER_BIT
            return
        ratio = self.devicePixelRatioF()
        fbo = self.defaultFramebufferObject()
        self.render_context.render(flip_y=True, opengl_fbo={"w": round(self.width() * ratio),
                                                            "h": round(self.height() * ratio), "fbo": fbo})
        # mpv leaves framebuffer 0 bound; Qt expects its own one back.
        functions.glBindFramebuffer(GL_FRAMEBUFFER, fbo)

    def _swapped(self) -> None:
        if self.render_context is not None:
            try:
                self.render_context.report_swap()
            except Exception:  # noqa: BLE001 - timing statistics only
                pass

    def free(self) -> None:
        """Release mpv's OpenGL resources (must happen before mpv itself is stopped)."""
        if self.render_context is None:
            return
        self.makeCurrent()
        try:
            self.render_context.free()
        finally:
            self.render_context = None
            self.doneCurrent()


class MpvPlayer(QObject):
    playingChanged = Signal(bool)
    loaded = Signal()          # a new film is open (duration known)
    tracksChanged = Signal()
    ended = Signal()
    _from_mpv = Signal(str, object, float)   # (what, value, when it arrived): mpv's thread -> GUI thread

    engine = "mpv"

    def __init__(self, module, video_output: str, hwdec: str = "auto-safe", parent=None):
        super().__init__(parent)
        if video_output not in VIDEO_OUTPUTS:
            raise ValueError(f"unknown video output {video_output!r}")
        self.module = module
        self.video_output = video_output
        self.hwdec = hwdec
        self.core = None
        self.widget: Optional[QWidget] = None
        self.path: Optional[Path] = None
        self.version = ""
        self._pos = 0.0               # ms, as last reported by mpv
        self._pos_time = time.monotonic()
        self._paused = True
        self._idle = True             # mpv's core-idle: not moving forward (paused, loading, jumping)
        self._duration = 0
        self._loaded = False
        self._failed = False
        self._waiting = False         # loading or jumping: until mpv shows the picture there
        self._wait_kind = ""          # "load" or "jump"
        self._seek_seq = 0            # jumps asked for; the picture after the latest one counts once
        self._seek_acked = False      # mpv has taken the latest jump (later "playback-restart" = it's done)
        self._load_seq = 0            # how many films were asked for (each load() counts one up)
        self._wanted_entry = None     # mpv's playlist entry of the film asked for last
        self._current_entry = None    # the entry mpv is busy with: events about others are ignored
        self._target = 0.0
        self._wait_started = 0.0
        self._eof = False
        self._tracks: list = []
        self._volume, self._muted = 80, False
        self._last_playing = False
        self._last_child_check = 0.0
        self._reported: set = set()   # problem codes already shown for this film
        self._from_mpv.connect(self._on_mpv, Qt.ConnectionType.QueuedConnection)

    # ------------------------------------------------------------ setting up
    def video_widget(self, background: str, parent=None) -> QWidget:
        if self.video_output == "opengl":
            self.widget = GLVideo(self, background, parent)
        else:
            self.widget = NativeVideo(background, self.video_output == "window", parent)
        return self.widget

    def start(self) -> None:
        """Start mpv, after the video widget has its place in the window. Raises StartFailed."""
        options = {
            "idle": "yes", "keep_open": "yes", "pause": "yes", "hr_seek": "yes",
            "sid": "no", "sub_auto": "no",                 # SyncWatch draws subtitles itself
            "osc": "no", "osd_level": "0", "load_scripts": "no", "ytdl": "no", "load_stats_overlay": "no",
            "input_default_bindings": "no", "input_vo_keyboard": "no", "input_cursor": "no",
            "cursor_autohide": "no", "terminal": "no", "config": "no",
            "hwdec": self.hwdec, "volume": str(self._volume), "mute": "yes" if self._muted else "no",
            "audio_client_name": "SyncWatch",
        }
        if self.video_output == "window":
            options["wid"] = str(int(self.widget.winId()))
            options["force_window"] = "yes"   # the window exists right away (black), so it can be set up
        elif self.video_output == "opengl":
            options["vo"] = "libmpv"
        else:
            options["vo"], options["ao"], options["hwdec"] = "null", "null", "no"
            self.hwdec = "no"
        try:
            self.core = self.module.MPV(log_handler=self._log_from_mpv, loglevel="warn", **options)
        except Exception as exc:  # noqa: BLE001 - whatever libmpv says, we fall back
            raise StartFailed(errors.describe_exception(exc)) from exc
        try:
            self.core["drag-and-drop"] = "no"   # dropped files go to SyncWatch, not straight to mpv
        except Exception:  # noqa: BLE001 - option only exists in newer mpv versions
            pass
        self.version = mpvlib.version_text(self.core)
        self.core.register_event_callback(self._event_from_mpv)
        # Not "pause": SyncWatch decides that itself (mpv only pauses on its own at the end, eof-reached).
        for name in ("time-pos", "core-idle", "duration", "eof-reached", "track-list"):
            self.core.observe_property(name, self._property_from_mpv)
        log.info("mpv started: %s, video output %s, hardware decoding %s", self.version, self.video_output,
                 self.hwdec)

    def describe(self) -> str:
        """Short: 'mpv 0.41.0 · own window · hardware decoding on' (the log has the full version)."""
        found = re.search(r"\d+\.\d+(\.\d+)?", self.version)
        drawing = {"window": "own window", "opengl": "OpenGL", "none": "no picture (tests)"}[self.video_output]
        decoding = "off" if self.hwdec == "no" else "on"
        return f"mpv {found.group(0) if found else '?'} · {drawing} · hardware decoding {decoding}"

    def set_hwdec(self, value: str) -> None:
        self.hwdec = value
        self._async("set", "hwdec", value)

    def shutdown(self) -> None:
        """Stop mpv (when the window closes). Safe to call twice."""
        core, self.core = self.core, None
        if core is None:
            return
        if isinstance(self.widget, GLVideo):
            self.widget.free()
        try:
            core.terminate()
        except Exception:  # noqa: BLE001 - closing anyway
            log.exception("mpv didn't stop cleanly")

    # ------------------------------------------------------------ what sync needs
    def position(self) -> float:
        if self._waiting:
            return self._target   # mpv holds still until the picture at the new spot is ready
        if not self.is_playing() or self._idle:
            return self._pos
        ahead = (time.monotonic() - self._pos_time) * 1000.0
        return self._pos + min(max(0.0, ahead), MAX_EXTRAPOLATE_MS)

    def duration(self) -> int:
        return self._duration

    def is_playing(self) -> bool:
        return self.has_media() and not self._paused and not self._eof

    def has_media(self) -> bool:
        return self.path is not None and self._loaded and not self._failed

    def is_ready(self) -> bool:
        return self.has_media() and not self._waiting

    def play(self) -> None:
        if not self.has_media():
            return
        self._pos_time = time.monotonic()
        self._paused = False
        self._async("set", "pause", "no")
        self._report_playing()

    def pause(self) -> None:
        if not self.has_media():
            return
        self._pos, self._pos_time = self.position(), time.monotonic()   # never "jump back" when pausing
        self._paused = True
        self._async("set", "pause", "yes")
        self._report_playing()

    def seek(self, ms: float) -> None:
        if not self.has_media():
            return
        ms = max(0.0, float(ms))
        self._waiting, self._wait_kind, self._target, self._wait_started = True, "jump", ms, time.monotonic()
        self._pos, self._pos_time = ms, time.monotonic()
        self._eof = False
        self._seek_seq += 1
        self._seek_acked = False
        seq = self._seek_seq
        try:
            self.core.command_async("seek", f"{ms / 1000.0:.3f}", "absolute+exact",
                                    callback=lambda error, result: self._send("seek-reply", (seq, error)))
        except Exception as exc:  # noqa: BLE001 - mpv shutting down: don't wait for a jump that won't come
            log.warning("mpv didn't take the jump: %s", exc)
            self._waiting = False

    # ------------------------------------------------------------ loading
    def load(self, path: Path) -> bool:
        if self.core is None:
            return False
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
        self._loaded = self._failed = self._eof = False
        self._paused = True
        self._waiting, self._wait_kind, self._target, self._wait_started = True, "load", 0.0, time.monotonic()
        self._pos, self._pos_time = 0.0, time.monotonic()
        self._duration = 0
        self._tracks = []
        self._reported.clear()
        self._load_seq += 1
        self._wanted_entry = None
        self._async("set", "pause", "yes")          # open paused: shows the first picture
        seq = self._load_seq
        try:
            self.core.command_async("loadfile", str(path), "replace",
                                    callback=lambda error, result: self._send("loadfile-reply", (seq, error, result)))
        except Exception as exc:  # noqa: BLE001 - mpv refused straight away
            errors.report("SW-205", f"mpv: {errors.describe_exception(exc)}")
            return False
        self._report_playing()
        log.info("loading film %s (%d MB)", path.name, path.stat().st_size // 1_000_000)
        return True

    def unload(self) -> None:
        self._async("stop")
        self.path = None
        self._loaded = False
        self._waiting = False
        self._report_playing()

    # ------------------------------------------------------------ sound
    def set_volume(self, percent: int, muted: bool) -> None:
        self._volume, self._muted = max(0, min(100, int(percent))), bool(muted)
        self._async("set", "volume", str(self._volume))
        self._async("set", "mute", "yes" if self._muted else "no")

    def has_audio_device(self) -> bool:
        from PySide6.QtMultimedia import QMediaDevices
        return bool(QMediaDevices.audioOutputs())

    # ------------------------------------------------------------ tracks
    def _audio(self) -> list:
        return [t for t in self._tracks if isinstance(t, dict) and t.get("type") == "audio"]

    def audio_tracks(self) -> List[Tuple[int, str, str]]:
        """[(index, label, language code)]"""
        tracks = []
        for index, track in enumerate(self._audio()):
            code, name = language(str(track.get("lang") or ""))
            title = str(track.get("title") or "")
            bits = [f"Track {index + 1}"]
            if name:
                bits.append(name)
            codec = str(track.get("codec") or "").upper()
            channels = CHANNELS.get(track.get("demux-channel-count"), "")
            if codec or channels:
                bits.append(" ".join(b for b in (codec, channels) if b))
            if title and title.lower() != name.lower():
                bits.append(title)
            tracks.append((index, " · ".join(bits), code))
        return tracks

    def active_audio_track(self) -> int:
        for index, track in enumerate(self._audio()):
            if track.get("selected"):
                return index
        return -1

    def set_audio_track(self, index: int) -> None:
        audio = self._audio()
        if 0 <= index < len(audio) and audio[index].get("id") is not None:
            self._async("set", "aid", str(audio[index]["id"]))

    # ------------------------------------------------------------ the clock
    def tick(self) -> None:
        """Called by the UI timer."""
        now = time.monotonic()
        if self._waiting and now - self._wait_started > WAIT_GIVE_UP_S:
            log.info("mpv didn't report the picture %.0f s after a jump; continuing anyway", WAIT_GIVE_UP_S)
            self._finish_waiting(now)
        if (self.video_output == "window" and sys.platform == "win32" and self.widget is not None
                and now - self._last_child_check > CHILD_WINDOW_CHECK_S):
            self._last_child_check = now
            if winfix.disable_child_windows(int(self.widget.winId())):
                log.info("mpv's window now leaves the mouse to SyncWatch")

    # ------------------------------------------------------------ talking to mpv
    def _async(self, *command: str) -> None:
        if self.core is None:
            return
        try:
            self.core.command_async(*command)
        except Exception as exc:  # noqa: BLE001 - mpv shutting down or refusing: log, don't crash
            log.warning("mpv command %s failed: %s", command[0], exc)

    def _event_from_mpv(self, event) -> None:
        """mpv's thread."""
        ids = self.module.MpvEventID
        kind = event.event_id.value
        if kind == ids.START_FILE:
            self._send("start-file", event.data.playlist_entry_id)
        elif kind == ids.FILE_LOADED:
            self._send("file-loaded", None)
        elif kind == ids.PLAYBACK_RESTART:
            self._send("playback-restart", None)
        elif kind == ids.END_FILE:
            data = event.data
            self._send("end-file", (data.reason, data.error, data.playlist_entry_id))

    def _property_from_mpv(self, name: str, value) -> None:
        """mpv's thread."""
        self._send(name, value)

    def _send(self, what: str, value) -> None:
        """mpv's thread: hand it to the GUI thread (stamped with the time it arrived)."""
        try:
            self._from_mpv.emit(what, value, time.monotonic())
        except RuntimeError:
            pass   # the window is gone (closing): nobody needs it anymore

    def _log_from_mpv(self, level: str, prefix: str, text: str) -> None:
        """mpv's thread: mpv's warnings go to SyncWatch's log; some become clear error messages."""
        text = text.strip()
        core_log.log(logging.ERROR if level in ("fatal", "error") else logging.WARNING, "[%s] %s", prefix, text)
        if "Failed to initialize a decoder" in text or "Could not initialize video chain" in text:
            self._send("decoder-failed", (prefix, text))
        elif "selected video_out" in text or "Failed initializing any suitable GPU context" in text:
            self._send("vo-failed", text)
        elif "Could not open/initialize audio device" in text:
            self._send("ao-failed", text)

    def _on_mpv(self, what: str, value, when: float) -> None:
        """GUI thread: everything mpv reported."""
        if what == "time-pos":
            if value is not None and not self._waiting:
                self._pos, self._pos_time = float(value) * 1000.0, when
        elif what == "core-idle":
            idle = bool(value)
            if idle != self._idle:
                if not idle:
                    self._pos_time = when   # moving again: estimate from now on
                self._idle = idle
        elif what == "duration":
            self._duration = max(0, int(float(value) * 1000)) if value else 0
        elif what == "eof-reached":
            eof = bool(value)
            if eof and not self._eof:
                self._eof = True
                self._pos, self._pos_time = self.position(), when
                self._paused = True       # keep-open: mpv pauses at the end by itself
                self._report_playing()
                self.ended.emit()
            elif not eof:
                self._eof = False
        elif what == "track-list":
            self._tracks = list(value or [])
            self.tracksChanged.emit()
        elif what == "loadfile-reply":
            seq, error, result = value
            if seq != self._load_seq:
                return   # a film that was replaced in the meantime
            if error:
                self._failed, self._waiting = True, False
                self._problem("SW-205", f"mpv refused the file: {error}")
                self._report_playing()
            else:
                self._wanted_entry = (result or {}).get("playlist_entry_id") if isinstance(result, dict) else None
        elif what == "start-file":
            self._current_entry = value
        elif what in ("file-loaded", "playback-restart") and (
                self._wanted_entry is None or self._current_entry != self._wanted_entry):
            return   # about a film that was replaced in the meantime
        elif what == "file-loaded":
            if self.path is None:
                return
            self._loaded = True
            duration = self._read("duration")
            if duration:
                self._duration = max(0, int(float(duration) * 1000))
            self.loaded.emit()
            self._report_playing()
        elif what == "seek-reply":
            seq, error = value
            if seq == self._seek_seq and self._wait_kind == "jump":
                self._seek_acked = True
                if error:   # e.g. a file that can't jump: there's no picture to wait for
                    log.info("mpv couldn't jump: %s", error)
                    self._finish_waiting(when)
        elif what == "playback-restart":
            # After a load: the first picture. After a jump: the picture at the new spot, but only
            # once mpv has taken the latest jump (a restart from an earlier jump doesn't count).
            if self._waiting and (self._wait_kind == "load" or self._seek_acked):
                self._finish_waiting(when)
        elif what == "end-file":
            reason, error, entry = value
            if entry != self._wanted_entry:
                return   # about a film that was replaced in the meantime
            if reason == self.module.MpvEventEndFile.ERROR and self.path is not None:
                self._failed = True
                self._waiting = False
                self._problem("SW-205", f"mpv: {self._error_text(error)}")
                self._report_playing()
        elif what == "decoder-failed":
            prefix, text = value
            self._problem("SW-212" if prefix == "ad" else "SW-205", f"mpv [{prefix}]: {text}")
        elif what == "vo-failed":
            self._problem("SW-208", f"mpv ({self.video_output}): {value}")
        elif what == "ao-failed":
            self._problem("SW-210", f"mpv: {value}", popup=False)   # the startup checks already say so

    def _finish_waiting(self, when: float, position=None) -> None:
        if position is None:
            position = self._read("time-pos")
        self._waiting = False
        if position is not None:
            self._pos = float(position) * 1000.0
        self._pos_time = when

    def _read(self, name: str):
        if self.core is None:
            return None
        try:   # (python-mpv: attributes are mpv's properties; core["x"] would be the option "x")
            return getattr(self.core, name.replace("-", "_"))
        except Exception:  # noqa: BLE001 - property not available right now
            return None

    def _error_text(self, code: int) -> str:
        try:
            return self.module._mpv_error_string(code).decode("utf-8", "replace")
        except Exception:  # noqa: BLE001 - only for the details text
            return f"error {code}"

    def _problem(self, code: str, technical: str, popup=None) -> None:
        if code in self._reported:
            return
        self._reported.add(code)
        errors.report(code, technical, popup=popup)

    def _report_playing(self) -> None:
        playing = self.is_playing()
        if playing != self._last_playing:
            self._last_playing = playing
            self.playingChanged.emit(playing)
