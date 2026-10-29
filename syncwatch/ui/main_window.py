"""The main window: video on the left, sidebar on the right, controls underneath.

It wires everything together: the player, the sync controller, subtitles,
the overlay, settings and the error popups. Most of the actual logic lives in
those parts; this file mostly connects them.
"""
from __future__ import annotations

import logging
import queue
import time
from pathlib import Path
from typing import List, Optional

from PySide6.QtCore import QEvent, QPoint, QRect, Qt, QTimer, Signal
from PySide6.QtCore import QBuffer, QByteArray, QIODevice
from PySide6.QtGui import (QActionGroup, QColor, QCursor, QGuiApplication, QIcon, QImage, QKeySequence,
                           QPainter, QShortcut)
from PySide6.QtWidgets import (QApplication, QFileDialog, QFrame, QHBoxLayout, QLabel, QMainWindow, QMessageBox,
                               QSplitter, QStackedWidget, QVBoxLayout, QWidget)

from .. import errors, log as log_module, paths
from ..filmstate import film_key
from ..settings import RESTART_KEYS, SCHEMA
from ..sync.controller import Controller, fmt_time
from .controls import ControlBar
from .dialogs import error_dialog
from .dialogs.avatar_editor import choose_avatar
from .dialogs.settings_dialog import (SettingsDialog, diagnostics_extras, profile_extras, rooms_extras,
                                      updates_extras)
from .dialogs.subtitle_timing import SubtitleTimingDialog
from .library_ui import LibraryController, LibraryPanel
from . import players, winfix
from .overlay import Overlay, SubtitleStyle
from .player import VIDEO_EXTENSIONS
from .qtutil import asset_pixmap
from .sidebar import FriendEntry, PersonEntry, Sidebar, WaitingEntry
from .subtitles_ui import Subtitles
from .updates_ui import UpdateController

log = logging.getLogger("syncwatch.window")

TICK_MS = 50
SAVE_EVERY_MS = 2000
RESUME_SAVE_S = 5.0
IN_SYNC_MS = 300


class Background(QWidget):
    """Paints assets/background.png behind everything (how: theme.json 'background.mode')."""

    def __init__(self, theme, parent=None):
        super().__init__(parent)
        self.theme = theme
        self.pixmap = asset_pixmap(theme.background_image)

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(self.theme.colors["window"][:7]))
        pixmap = self.pixmap
        if pixmap is not None and not pixmap.isNull():
            mode = self.theme.background_mode
            if mode == "tile":
                painter.drawTiledPixmap(self.rect(), pixmap)
            elif mode == "stretch":
                painter.drawPixmap(self.rect(), pixmap)
            elif mode == "center":
                painter.drawPixmap((self.width() - pixmap.width()) // 2, (self.height() - pixmap.height()) // 2, pixmap)
            else:  # cover
                scaled = pixmap.scaled(self.size(), Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                                       Qt.TransformationMode.SmoothTransformation)
                painter.drawPixmap((self.width() - scaled.width()) // 2, (self.height() - scaled.height()) // 2, scaled)
        painter.end()


class IdleScreen(QWidget):
    """What you see before a film is open: your films (the library), on a card in the middle."""
    CARD_WIDTH = 720
    LIST_ROWS = 10      # the list grows with the films up to this many rows, then scrolls

    def __init__(self, parent=None):
        super().__init__(parent)
        logo = QLabel()
        pixmap = asset_pixmap("logo.png")
        if pixmap is not None:
            ratio = self.devicePixelRatioF()
            scaled = pixmap.scaled(round(56 * ratio), round(56 * ratio), Qt.AspectRatioMode.KeepAspectRatio,
                                   Qt.TransformationMode.SmoothTransformation)
            scaled.setDevicePixelRatio(ratio)
            logo.setPixmap(scaled)
        logo.setAlignment(Qt.AlignmentFlag.AlignCenter)
        card = QFrame()
        card.setObjectName("Card")
        self.library = LibraryPanel(fit_rows=self.LIST_ROWS)
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(18, 16, 18, 14)
        card_layout.addWidget(self.library)
        self.hint = QLabel("Tip: drop a film file here to play it straight away, without adding it to the library.")
        self.hint.setObjectName("Hint")
        self.hint.setWordWrap(True)
        self.hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        column = QWidget()
        column.setMaximumWidth(self.CARD_WIDTH)
        column_layout = QVBoxLayout(column)
        column_layout.setContentsMargins(0, 0, 0, 0)
        column_layout.setSpacing(14)
        column_layout.addWidget(logo)
        column_layout.addWidget(card)
        column_layout.addWidget(self.hint)
        row = QHBoxLayout()   # the column as wide as it may be, the rest split evenly on both sides
        row.addStretch(1)
        row.addWidget(column, 30)
        row.addStretch(1)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.addStretch(2)
        layout.addLayout(row)
        layout.addStretch(3)   # a little above the middle looks centred to the eye


class MainWindow(QMainWindow):
    _problem_signal = Signal(object)

    def __init__(self, context):
        super().__init__()
        self.ctx = context
        self.settings = context.settings
        self.films = context.films
        self.setWindowTitle("SyncWatch")
        self.setAttribute(Qt.WidgetAttribute.WA_AlwaysShowToolTips)
        icon = asset_pixmap("logo.png")
        if icon is not None:
            self.setWindowIcon(QIcon(icon))
        self.resize(1280, 760)
        self.setAcceptDrops(True)

        self.inbox: "queue.Queue[dict]" = queue.Queue()
        self.player = players.create(self.settings, self)   # mpv, or Qt's player as the fallback
        self.subtitles = Subtitles(self.settings, self.films, context.profile.cache_dir, self)
        self.avatar: Optional[bytes] = context.avatar

        self._film_key = ""
        self._last_resume_save = 0.0
        self._refresh_wanted = True
        self._was_maximized = False
        self._last_mouse = QPoint()
        self._last_mouse_move = time.monotonic()
        self._cursor_hidden = False
        self._shortcuts: List[QShortcut] = []
        self._gap_text, self._gap_kind = "", ""
        self._dialogs: list = []
        self._avatars: dict = {}   # key id -> picture of people met in rooms (for the friends list)
        self.settings_dialog_refresh = None  # set while the settings window is open

        self._build()   # also starts the player (and switches to Qt's if mpv can't start)
        self.controller = Controller(self.player, self, self.settings, self.inbox.put, context.rooms)
        self.controller.set_profile(self.settings.get("profile.name"), self.avatar)
        self.library = LibraryController(self)
        self.library.attach(self.idle.library)
        self._wire()
        self._apply_all_settings()
        self._restore_geometry()

        self.tick_timer = QTimer(self)
        self.tick_timer.setInterval(TICK_MS)
        self.tick_timer.timeout.connect(self._tick)
        self.tick_timer.start()
        self.save_timer = QTimer(self)
        self.save_timer.setInterval(SAVE_EVERY_MS)
        self.save_timer.timeout.connect(self._save_state)
        self.save_timer.start()
        self.gap_timer = QTimer(self)
        self.gap_timer.timeout.connect(self._refresh_gap)
        self._set_gap_rate()
        self.gap_timer.start()

        self.updates = UpdateController(self)
        self.sidebar.update_banner.clicked.connect(self._update_banner_clicked)
        QTimer.singleShot(0, self.updates.startup)
        QTimer.singleShot(0, self.controller.start)  # listen for friends' invitations

    # ================================================================ building
    def _build(self) -> None:
        theme = self.ctx.theme
        self.background = Background(theme)
        self.setCentralWidget(self.background)

        self.idle = IdleScreen()
        self.stack = QStackedWidget()
        self.stack.addWidget(self.idle)
        self.video: Optional[QWidget] = None
        self._place_video(self.player)
        self.player = players.start_or_fallback(self.player, self._place_video, self)

        self.controls = ControlBar(theme.colors)
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(8, 8, 4, 8)
        left_layout.setSpacing(6)
        left_layout.addWidget(self.stack, 1)
        left_layout.addWidget(self.controls)

        self.sidebar = Sidebar(self.settings.get("room.last_code"))
        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.splitter.addWidget(left)
        self.splitter.addWidget(self.sidebar)
        self.splitter.setStretchFactor(0, 1)
        self.splitter.setStretchFactor(1, 0)
        self.splitter.setCollapsible(0, False)
        self.splitter.setSizes([960, 300])
        outer = QHBoxLayout(self.background)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(self.splitter)

        self.overlay = Overlay(self)
        QApplication.instance().installEventFilter(self)

    def _place_video(self, player) -> None:
        """Put the player's picture into the video area (replacing a previous player's)."""
        if self.video is not None:
            self.stack.removeWidget(self.video)
            self.video.deleteLater()
        self.video = player.video_widget(self.ctx.theme.colors["video_background"][:7], self.stack)
        self.stack.addWidget(self.video)

    def _wire(self) -> None:
        c, p, ui = self.controller, self.player, self.controls
        ui.playClicked.connect(c.toggle)
        ui.backClicked.connect(lambda: c.skip(-1000 * self.settings.get("playback.skip_s")))
        ui.forwardClicked.connect(lambda: c.skip(1000 * self.settings.get("playback.skip_s")))
        ui.syncUpClicked.connect(c.sync_up)
        ui.syncMeClicked.connect(c.sync_me)
        ui.followToggled.connect(c.set_attached)
        ui.muteClicked.connect(self.toggle_mute)
        ui.volumeChanged.connect(lambda v: self.settings.set("playback.volume", v))
        ui.fullscreenClicked.connect(self.toggle_fullscreen)
        ui.sidebarClicked.connect(self.toggle_sidebar)
        ui.settingsClicked.connect(self.open_settings)
        ui.openClicked.connect(self.open_library)
        ui.seek.seekRequested.connect(c.seek)
        ui.seek.previewChanged.connect(lambda ms: ui.time_label.setText(fmt_time(ms)))
        ui.subs_menu.aboutToShow.connect(self._build_subs_menu)

        room = self.sidebar.room
        room.hostClicked.connect(self._host)
        room.joinClicked.connect(self._join)
        room.leaveClicked.connect(self._leave)
        room.copyClicked.connect(self._copy_code)
        room.newCodeClicked.connect(self._new_code)
        room.approveClicked.connect(self.controller.approve)
        room.approveAllClicked.connect(self.controller.approve_all)
        room.denyClicked.connect(self._deny)
        self.sidebar.people.actionRequested.connect(self._person_action)
        friends = self.sidebar.friends
        friends.joinClicked.connect(self._friend_join)
        friends.acceptClicked.connect(lambda key: self.controller.friends.accept(self.controller.session, key))
        friends.declineClicked.connect(lambda key: self.controller.friends.decline(self.controller.session, key))
        friends.removeRequested.connect(self._friend_remove)

        p.loaded.connect(self._film_loaded)
        p.playingChanged.connect(self._playing_changed)
        p.tracksChanged.connect(self._apply_audio_preference)
        self.subtitles.statusChanged.connect(lambda text: self._refresh_soon())
        self._problem_signal.connect(lambda report: error_dialog.show(report, self.ctx.profile.logs_dir, self))
        errors.set_handler(self._problem_signal.emit)  # thread-safe: the signal hops to the GUI thread

        self.settings.subscribe("subs.", lambda k, v: self._apply_subtitle_style())
        self.settings.subscribe("keys.", lambda k, v: self._install_shortcuts())
        self.settings.subscribe("ui.", lambda k, v: self._apply_ui_settings())
        self.settings.subscribe("playback.volume", lambda k, v: self._apply_volume())
        self.settings.subscribe("playback.muted", lambda k, v: self._apply_volume())
        self.settings.subscribe("log.debug", lambda k, v: log_module.set_debug(bool(v)))
        self.settings.subscribe("profile.name", lambda k, v: self._profile_changed())
        self.settings.subscribe("sync.gap_refresh_s", lambda k, v: self._set_gap_rate())
        self.settings.subscribe("playback.hwdec", lambda k, v: self.player.set_hwdec(v))

    def _apply_all_settings(self) -> None:
        self._apply_subtitle_style()
        self._install_shortcuts()
        self._apply_ui_settings()
        self._apply_volume()

    # ================================================================ the controller's UI interface
    def status(self, text: str, connected: bool) -> None:
        self.sidebar.room.set_status(text, connected)
        self._refresh_soon()

    def activity(self, text: str, kind: str = "info") -> None:
        self.sidebar.activity.add(text, kind)
        if self.settings.get("ui.toasts") and kind in ("friend", "warn", "error"):
            self.overlay.toast(text, kind)

    def problem(self, code: str, technical: str = "", **params) -> None:
        report = errors.report(code, technical, **params)
        if not report.problem.popup:
            text = report.title if report.explanation.startswith("{") else f"{report.title}. {report.explanation}"
            kind = "warn" if report.problem.severity != "info" else "info"
            self.sidebar.activity.add(text, kind)
            if self.settings.get("ui.toasts"):
                self.overlay.toast(text, kind)

    def countdown(self, ms: int) -> None:
        self.overlay.countdown(ms)

    def changed(self) -> None:
        self._refresh_soon()

    def join_request(self, knock) -> None:
        """Someone wants into my room: make sure I notice, even when SyncWatch is in the background."""
        QApplication.alert(self)

    def shrink_avatar(self, data: bytes, max_bytes: int) -> Optional[bytes]:
        """A smaller copy of a picture that fits into one room message."""
        image = QImage.fromData(data)
        if image.isNull():
            return None
        for size in (128, 96, 72, 56, 40):
            scaled = image.scaled(size, size, Qt.AspectRatioMode.KeepAspectRatio,
                                  Qt.TransformationMode.SmoothTransformation)
            buffer = QBuffer()
            buffer.open(QIODevice.OpenModeFlag.WriteOnly)
            scaled.save(buffer, "PNG")
            result = bytes(buffer.data())
            if len(result) <= max_bytes:
                return result
        return None

    # ================================================================ rooms
    def _confirm(self, title: str, text: str, yes: str = "Yes") -> bool:
        box = QMessageBox(QMessageBox.Icon.Question, title, text, parent=self)
        box.setTextFormat(Qt.TextFormat.PlainText)   # names come from other people: never as HTML
        ok = box.addButton(yes, QMessageBox.ButtonRole.AcceptRole)
        box.addButton("Cancel", QMessageBox.ButtonRole.RejectRole)
        box.exec()
        return box.clickedButton() is ok

    def _host(self) -> None:
        self.controller.host_room()
        self._refresh_soon()

    def _join(self, text: str) -> None:
        if self.controller.join_room(text):
            self.settings.set("room.last_code", text.strip()[:600])
        self._refresh_soon()

    def _leave(self) -> None:
        c = self.controller
        if c.is_host and c.peers and not self._confirm(
                "Close your room?", f"{len(c.peers)} {'person is' if len(c.peers) == 1 else 'people are'} "
                                    "watching with you. Closing the room ends it for everyone.", "Close room"):
            return
        c.leave()
        self._refresh_soon()

    def _copy_code(self) -> None:
        code = self.controller.session.code
        if code is not None:
            QGuiApplication.clipboard().setText(code.text())
            self.activity("Room code copied. Paste it to your friends (Ctrl+V).", "you")

    def _new_code(self) -> None:
        if self._confirm("Make a new room code?",
                         "The old code stops working. People in your room now follow automatically; "
                         "anyone else needs the new code (friends get it in their Friends list).", "New code"):
            self.controller.new_code()
            self._refresh_soon()

    def _deny(self, key: bytes, block: bool) -> None:
        knock = self.controller.session.waiting.get(key)
        if block and knock is not None and not self._confirm(
                "Block this person?", f"{knock.name} ({knock.key.short_id}) won't be able to ask to join your "
                                      "room again. You can unblock them in Settings > Watching together.", "Block"):
            return
        self.controller.deny(key, block)

    def _person_action(self, action: str, key: bytes) -> None:
        c = self.controller
        if action == "copy_my_id":
            QGuiApplication.clipboard().setText(c.rooms.identity.public.short_id)
            self.activity("Your ID copied", "you")
            return
        member = c.session.member(key)
        if member is None:
            return
        if action == "friend":
            c.friends.request(c.session, member)
        elif action == "kick" and self._confirm("Remove from the room?",
                                                f"{member.name} will be removed. They can ask to join again.",
                                                "Remove"):
            c.kick(key)
        elif action == "block" and self._confirm("Remove and block?",
                                                 f"{member.name} ({member.key.short_id}) will be removed and can't "
                                                 "ask to join again. You can unblock them in Settings > Watching "
                                                 "together.", "Remove and block"):
            c.kick(key, ban=True)
        elif action == "copy_id":
            QGuiApplication.clipboard().setText(member.key.short_id)
            self.activity(f"{member.name}'s ID copied", "you")
        self._refresh_soon()

    def _friend_join(self, key: bytes) -> None:
        room = self.controller.friends.room_of(key)
        if room is not None:
            self.controller.join_code(room.code)
        self._refresh_soon()

    def _friend_remove(self, key: bytes) -> None:
        friend = self.controller.rooms.store.friends.get(key)
        if friend is not None and self._confirm("Remove friend?", f"Remove {friend.name} from your friends? "
                                                                  "You'll disappear from their list too.", "Remove"):
            self.controller.friends.remove(key)
        self._refresh_soon()

    def _avatar_of(self, key: bytes) -> Optional[bytes]:
        if key not in self._avatars:
            path = self.ctx.profile.cache_dir / "avatars" / f"{key.hex()}.png"
            try:
                self._avatars[key] = path.read_bytes() if path.is_file() and path.stat().st_size < 200_000 else None
            except OSError:
                self._avatars[key] = None
        return self._avatars[key]

    def _remember_avatar(self, key: bytes, data: Optional[bytes]) -> None:
        if not data or self._avatars.get(key) == data:
            return
        self._avatars[key] = data
        folder = self.ctx.profile.cache_dir / "avatars"
        try:
            folder.mkdir(parents=True, exist_ok=True)
            (folder / f"{key.hex()}.png").write_bytes(data)
        except OSError:
            pass

    def _profile_changed(self) -> None:
        self.controller.set_profile(self.settings.get("profile.name"), self.avatar)
        self._refresh_soon()

    def set_avatar(self, data: Optional[bytes]) -> None:
        self.avatar = data
        path = self.ctx.profile.avatar_file
        try:
            if data is None:
                if path.exists():
                    path.unlink()
            else:
                tmp = path.with_suffix(".tmp")
                tmp.write_bytes(data)
                tmp.replace(path)
        except OSError as exc:
            errors.report("SW-105", errors.describe_exception(exc))
        self._profile_changed()

    # ================================================================ films
    def movies_dir(self) -> Path:
        return paths.movies_dir(self.settings.get("paths.movies_dir"))

    def pick_startup_film(self) -> None:
        """At startup without a film to open: the start screen shows the library."""
        self.library.refresh()

    def open_library(self) -> None:
        """📂 / Ctrl+O: the start screen's list when no film is open (or Add film if it's empty),
        else the Library window."""
        if self.stack.currentWidget() is not self.idle:
            self.library.open_dialog()
        elif self.idle.library.tree.topLevelItemCount():
            self.idle.library.tree.setFocus()
        else:
            self.library.add_film()

    def open_film(self, path: Path) -> None:
        self._save_resume(force=True)
        if self.player.load(path):
            try:
                size = path.stat().st_size
            except OSError:
                size = 0
            self._film_key = film_key(path.name, size)
            self.setWindowTitle(f"SyncWatch: {path.name}")
            self.stack.setCurrentWidget(self.video)
            self.subtitles.film_opened(path, self._film_key)
            self.controller.set_film(path.name, size, 0)

    def close_film(self) -> None:
        """Close the open film (e.g. before it's removed from the library): back to the start screen."""
        self._save_resume(force=True)
        self.player.unload()
        self.subtitles.film_closed()
        self._film_key = ""
        self.setWindowTitle("SyncWatch")
        self.stack.setCurrentWidget(self.idle)
        self.controller.set_film("", 0, 0)
        self._refresh_soon()

    def _film_loaded(self) -> None:
        path = self.player.path
        if path is None:
            return
        duration = self.player.duration()
        memory = self.films.get(self._film_key)
        resume = int(memory.get("pos_ms", 0))
        minimum = 1000 * int(self.settings.get("playback.resume_min_s"))
        if (self.settings.get("playback.resume") and not self.controller.connected
                and minimum < resume < duration - 60_000):
            self.player.seek(resume)
            self.activity(f"Continuing where you stopped ({fmt_time(resume)})", "you")
        self._apply_audio_preference()
        self.controller.set_film(path.name, path.stat().st_size if path.exists() else 0, duration)
        self.activity(f"Opened {path.name} ({fmt_time(duration)})", "you")
        self._refresh_soon()

    def _apply_audio_preference(self) -> None:
        tracks = self.player.audio_tracks()
        if len(tracks) < 2:
            return
        memory = self.films.get(self._film_key)
        if "audio_track" in memory:
            self.player.set_audio_track(int(memory["audio_track"]))
            return
        preferred = self.settings.languages("playback.audio_languages")
        for language in preferred:
            for index, _label, code in tracks:
                if code == language:
                    self.player.set_audio_track(index)
                    return

    def _save_resume(self, force: bool = False) -> None:
        if not self._film_key or not self.player.has_media():
            return
        now = time.monotonic()
        if not force and now - self._last_resume_save < RESUME_SAVE_S:
            return
        self._last_resume_save = now
        position = int(self.player.position())
        if position > self.player.duration() - 60_000:
            position = 0  # finished: start from the beginning next time
        self.films.update(self._film_key, pos_ms=position)

    def _save_state(self) -> None:
        if self.player.is_playing():
            self._save_resume()
        self.settings.save_if_dirty()
        self.films.save_if_dirty()

    # ================================================================ the clock
    def _tick(self) -> None:
        while True:
            try:
                message = self.inbox.get_nowait()
            except queue.Empty:
                break
            try:
                self.controller.handle(message)
            except Exception as exc:  # one bad message mustn't stop the others (or the clock)
                errors.report("SW-901", exc=exc)
        try:
            self.controller.tick()
        except Exception as exc:  # keep the app alive; the error is shown and logged
            errors.report("SW-901", exc=exc)
        self.player.tick()
        position = self.player.position()
        duration = self.player.duration()
        if not self.controls.seek.dragging:
            self.controls.seek.set_state(position, duration)
            self.controls.set_times(position, duration, self.settings.get("ui.remaining"),
                                    self.settings.get("ui.end_clock"))
        if self.settings.get("subs.enabled"):
            self.overlay.set_subtitles(self.subtitles.cues_at(position))
        else:
            self.overlay.set_subtitles([])
        self._follow_video_area()
        self._fullscreen_autohide()
        if self._refresh_wanted:
            self._refresh_wanted = False
            self._refresh_people()

    def _refresh_soon(self) -> None:
        self._refresh_wanted = True

    def _refresh_people(self) -> None:
        c = self.controller
        s = c.session
        connected = c.connected
        role = "host" if s.is_host else ("member" if connected else ("joining" if c.joining else "none"))
        waiting = [WaitingEntry(k.id, k.name, k.key.short_id, k.friend, k.friend_name) for k in s.waiting.values()]
        self.sidebar.room.set_state(role, s.code.text() if s.is_host and s.code else "", waiting)
        self.sidebar.room.set_status(c.status_text, connected)

        me_status = "Playing" if self.player.is_playing() else ("Paused" if self.player.has_media() else "No film open")
        if connected and not c.attached:
            me_status = "Watching on your own"
        me_detail = me_status
        if connected and self.settings.get("sync.show_gap") and self._gap_text:
            me_detail += f" · {self._gap_text}"
        me_name = f"{self.settings.get('profile.name') or 'You'} (you)" + (" · host" if s.is_host else "")
        entries = [PersonEntry(b"", me_name, me_detail, self.avatar, f"Your ID: {c.rooms.identity.public.short_id}",
                               [("Copy my ID", "copy_my_id")])]
        host_estimate = c.peer_estimate() if not c.is_host else None
        markers = []
        for peer in c.people() if connected else []:
            self._remember_avatar(peer.id, peer.avatar)
            if not peer.known:
                state = "Connecting…"
            elif peer.separate:
                state = "Watching on their own"
            elif not peer.ready:
                state = "Loading…"
            else:
                state = "Playing" if peer.playing else "Paused"
            if peer.known and not peer.separate and self.settings.get("sync.show_gap"):
                if c.is_host:
                    offset = c.peer_gap_ms(peer)
                elif not peer.is_host and host_estimate is not None:
                    estimate = c.estimate(peer)
                    offset = None if estimate is None else estimate - host_estimate
                else:
                    offset = None
                if offset is not None:
                    state += " · in sync" if abs(offset) < IN_SYNC_MS else f" · {offset / 1000:+.1f} s"
            if peer.match == "different":
                state += " · different film"
            if peer.is_host and self.settings.get("sync.show_ping") and s.rtt_ms():
                state += f" · ping {s.rtt_ms():.0f} ms"
            friend = c.friends.is_friend(peer.member.key)
            actions = [] if friend else [("Add friend", "friend")]
            if c.is_host:
                actions += [("Remove from the room", "kick"), ("Remove and block", "block")]
            actions.append(("Copy their ID", "copy_id"))
            tooltip = f"ID {peer.member.key.short_id}" + (" · your friend" if friend else "")
            entries.append(PersonEntry(peer.id, peer.name + (" · host" if peer.is_host else ""), state,
                                       peer.avatar, tooltip, actions))
            if self.settings.get("ui.markers") and peer.known and not peer.separate:
                estimate = c.estimate(peer)
                if estimate is not None:
                    markers.append((int(estimate), self.ctx.theme.colors["marker"][:7], peer.name))
        self.sidebar.people.update_people(entries)
        self.controls.seek.set_markers(markers)
        self._refresh_friends()
        rules = c.effective_rules()
        attention = False
        gap = c.gap_ms()
        if gap is not None and abs(gap) / 1000.0 > float(self.settings.get("sync.gap_warn_s")):
            attention = True
        self.controls.set_sync(connected, c.can_sync_up(), c.cooldown_left("sync"),
                               self._gap_text if self.settings.get("sync.show_gap") else "",
                               attention, self._gap_kind)
        self.controls.set_following(c.attached, connected and not c.is_host)
        self.controls.sync_up.setToolTip("Only the host can sync everyone right now" if (connected and not c.is_host and rules.host_only)
                                         else "Pause everyone, line up on the host, resume together")

    def _refresh_friends(self) -> None:
        c = self.controller
        friends = c.friends
        entries = [FriendEntry(r.id, r.name, "wants to be your friend", self._avatar_of(r.id), request=True,
                               short_id=r.key.short_id) for r in friends.incoming.values()]
        current = c.session.code
        for friend in friends.friend_list():
            key = friend.key.key_id
            room = friends.room_of(key)
            inside = room is not None and current is not None and current.secret == room.code.secret
            status = "You're in their room" if inside else (f"Hosting {room.room}" if room else "Not hosting")
            entries.append(FriendEntry(key, friend.name, status, self._avatar_of(key),
                                       can_join=room is not None and not inside, short_id=friend.key.short_id))
        self.sidebar.friends.update_friends(entries)

    def _refresh_gap(self) -> None:
        gap = self.controller.gap_ms()
        if gap is None or not self.controller.peers:
            self._gap_text, self._gap_kind = "", ""
        elif abs(gap) < IN_SYNC_MS:
            self._gap_text, self._gap_kind = "in sync", "Good"
        else:
            warn = abs(gap) / 1000.0 > float(self.settings.get("sync.gap_warn_s"))
            direction = "ahead of" if gap > 0 else "behind"
            self._gap_text = f"{abs(gap) / 1000:.1f} s {direction} {self.controller.reference_name()}"
            self._gap_kind = "Warn" if warn else "Dim"
        self._refresh_people()

    def _set_gap_rate(self) -> None:
        self.gap_timer.setInterval(1000 * int(self.settings.get("sync.gap_refresh_s")))

    # ================================================================ playback helpers
    def _playing_changed(self, playing: bool) -> None:
        self.controls.set_playing(playing)
        if self.settings.get("ui.center_flash") and self.player.has_media():
            self.overlay.flash("▶" if playing else "❚❚")
        self._refresh_soon()

    def toggle_mute(self) -> None:
        self.settings.set("playback.muted", not self.settings.get("playback.muted"))

    def change_volume(self, delta: int) -> None:
        self.settings.set("playback.volume", self.settings.get("playback.volume") + delta)
        if self.settings.get("playback.muted"):
            self.settings.set("playback.muted", False)

    def _apply_volume(self) -> None:
        volume, muted = int(self.settings.get("playback.volume")), bool(self.settings.get("playback.muted"))
        self.player.set_volume(volume, muted)
        self.controls.set_volume(volume, muted)

    # ================================================================ subtitles & audio menu
    def _build_subs_menu(self) -> None:
        menu = self.controls.subs_menu
        menu.clear()
        subs = self.subtitles
        title = menu.addAction("Subtitles")
        title.setEnabled(False)
        group = QActionGroup(menu)
        off = menu.addAction("Off")
        off.setCheckable(True)
        off.setChecked(subs.current_id == "off")
        off.triggered.connect(lambda: subs.select("off"))
        group.addAction(off)
        for source in subs.sources:
            action = menu.addAction(source.label + ("" if source.usable else " (can't be shown)"))
            action.setCheckable(True)
            action.setChecked(source.id == subs.current_id)
            action.triggered.connect(lambda _=False, s=source.id: subs.select(s))
            group.addAction(action)
        if not subs.sources:
            none = menu.addAction("No subtitles found next to the film")
            none.setEnabled(False)
        menu.addSeparator()
        load = menu.addAction("Load a subtitle file…")
        load.triggered.connect(self._load_subtitle_file)
        timing = menu.addAction(f"Timing… (delay {subs.delay_ms / 1000:+.1f} s)")
        timing.triggered.connect(self._subtitle_timing)
        show = menu.addAction("Show subtitles")
        show.setCheckable(True)
        show.setChecked(self.settings.get("subs.enabled"))
        show.triggered.connect(lambda on: self.settings.set("subs.enabled", on))
        if subs.status:
            status = menu.addAction(subs.status)
            status.setEnabled(False)
        tracks = self.player.audio_tracks()
        if tracks:
            menu.addSeparator()
            audio = menu.addMenu("Audio track")
            audio_group = QActionGroup(audio)
            active = self.player.active_audio_track()
            for index, label, _code in tracks:
                action = audio.addAction(label)
                action.setCheckable(True)
                action.setChecked(index == active)
                action.triggered.connect(lambda _=False, i=index: self._pick_audio(i))
                audio_group.addAction(action)

    def _pick_audio(self, index: int) -> None:
        self.player.set_audio_track(index)
        if self._film_key:
            self.films.update(self._film_key, audio_track=index)

    def _load_subtitle_file(self) -> None:
        folder = str(self.player.path.parent) if self.player.path else str(self.movies_dir())
        path, _ = QFileDialog.getOpenFileName(self, "Load subtitles", folder,
                                              "Subtitles (*.srt *.vtt *.ass *.ssa);;All files (*)")
        if path:
            self.subtitles.load_external(Path(path))

    def _subtitle_timing(self) -> None:
        dialog = SubtitleTimingDialog(self.subtitles, self)
        dialog.show()
        self._dialogs.append(dialog)

    def _apply_subtitle_style(self) -> None:
        s = self.settings
        self.overlay.set_style(SubtitleStyle(
            size_pct=s.get("subs.size_pct"), color=s.get("subs.color"), outline_px=s.get("subs.outline_px"),
            outline_color=s.get("subs.outline_color"), box=s.get("subs.box"), box_opacity=s.get("subs.box_opacity"),
            font_family=s.get("subs.font"), bold=s.get("subs.bold"), margin_pct=s.get("subs.margin_pct")))
        self.overlay.subtitles_enabled = bool(s.get("subs.enabled"))

    # ================================================================ updates
    def _update_banner_clicked(self) -> None:
        if self.updates.manager.staged():
            self.request_restart()
        else:
            self.updates.open_dialog()

    def request_restart(self) -> None:
        """Close and start again (to finish an update). app.main() does the actual restart."""
        self.ctx.restart = True
        self.close()

    # ================================================================ settings window
    def open_settings(self) -> None:
        extras = {
            "Profile": profile_extras(lambda: self.avatar, self._change_avatar, lambda: self.set_avatar(None)),
            "Watching together": rooms_extras(self.controller),
            "Updates": updates_extras(self.updates, self),
            "Diagnostics": diagnostics_extras(self.ctx.profile.logs_dir, self.copy_report, self.ctx.rerun_checks),
        }
        before = {key: self.settings.get(key) for key in RESTART_KEYS}
        dialog = SettingsDialog(self.settings, extras, self)
        dialog.exec()
        self.settings_dialog_refresh = None
        self.settings.save_if_dirty()
        if any(self.settings.get(key) != value for key, value in before.items()) and self._confirm(
                "Restart SyncWatch?", "The video player changes the next time SyncWatch starts. "
                                      "Restart it now?", "Restart now"):
            self.request_restart()

    def _change_avatar(self) -> None:
        data = choose_avatar(self)
        if data is not None:
            self.set_avatar(data)

    def copy_report(self) -> str:
        text = self.ctx.build_report(self)
        QGuiApplication.clipboard().setText(text)
        self.activity("Report copied to the clipboard", "info")
        return text

    def _apply_ui_settings(self) -> None:
        s = self.settings
        self.sidebar.apply_settings(s)
        if not self.isFullScreen():
            self.sidebar.setVisible(s.get("ui.sidebar"))
        self.controls.seek.show_hover_time = s.get("ui.hover_time")
        self.controls.seek.show_markers = s.get("ui.markers")
        self.overlay.toast_seconds = float(s.get("ui.toast_s"))
        self._refresh_soon()

    # ================================================================ keyboard
    def _install_shortcuts(self) -> None:
        for shortcut in self._shortcuts:
            shortcut.setEnabled(False)
            shortcut.deleteLater()
        self._shortcuts = []
        skip = lambda big: 1000 * self.settings.get("playback.big_skip_s" if big else "playback.skip_s")  # noqa: E731
        actions = {
            "keys.play_pause": self.controller.toggle,
            "keys.back": lambda: self.controller.skip(-skip(False)),
            "keys.forward": lambda: self.controller.skip(skip(False)),
            "keys.back_big": lambda: self.controller.skip(-skip(True)),
            "keys.forward_big": lambda: self.controller.skip(skip(True)),
            "keys.volume_up": lambda: self.change_volume(self.settings.get("playback.volume_step")),
            "keys.volume_down": lambda: self.change_volume(-self.settings.get("playback.volume_step")),
            "keys.mute": self.toggle_mute,
            "keys.fullscreen": self.toggle_fullscreen,
            "keys.sync_me": self.controller.sync_me,
            "keys.sync_up": self.controller.sync_up,
            "keys.subs_toggle": lambda: self.settings.set("subs.enabled", not self.settings.get("subs.enabled")),
            "keys.subs_earlier": lambda: self._nudge_subs(-1),
            "keys.subs_later": lambda: self._nudge_subs(1),
            "keys.sidebar": self.toggle_sidebar,
            "keys.open": self.open_library,
            "keys.settings": self.open_settings,
            "keys.help": self.toggle_help,
        }
        for key, action in actions.items():
            sequence = self.settings.get(key)
            if not sequence:
                continue
            shortcut = QShortcut(QKeySequence(sequence), self)
            shortcut.setContext(Qt.ShortcutContext.WindowShortcut)
            shortcut.activated.connect(action)
            self._shortcuts.append(shortcut)
        escape = QShortcut(QKeySequence("Esc"), self)
        escape.activated.connect(self._escape)
        self._shortcuts.append(escape)

    def _nudge_subs(self, direction: int) -> None:
        step = int(self.settings.get("subs.delay_step_ms"))
        self.subtitles.nudge(direction * step)
        self.overlay.toast(f"Subtitle delay {self.subtitles.delay_ms / 1000:+.2f} s", "info")

    def _escape(self) -> None:
        if self.overlay.help_visible:
            self.overlay.show_help(None)
        elif self.isFullScreen():
            self.toggle_fullscreen()

    def toggle_help(self) -> None:
        if self.overlay.help_visible:
            self.overlay.show_help(None)
            return
        lines = ["Keyboard shortcuts (change them in Settings > Keyboard)", ""]
        for setting in SCHEMA:
            if setting.kind == "keys" and self.settings.get(setting.key):
                lines.append(f"{self.settings.get(setting.key)}\t{setting.label}")
        lines += ["", "Double-click the video: fullscreen · Esc: close this"]
        self.overlay.show_help("\n".join(lines))

    # ================================================================ fullscreen & sidebar
    def toggle_sidebar(self) -> None:
        if self.isFullScreen():
            self.sidebar.setVisible(not self.sidebar.isVisible())
        else:
            self.settings.set("ui.sidebar", not self.settings.get("ui.sidebar"))

    def toggle_fullscreen(self) -> None:
        # Windows hides the overlay (subtitles!) behind a fullscreen OpenGL window without a border.
        border_fix = getattr(self.player, "video_output", "") == "opengl"
        if self.isFullScreen():
            if border_fix:
                winfix.set_border(int(self.winId()), False)
            self.showMaximized() if self._was_maximized else self.showNormal()
            self.controls.setVisible(True)
            self.sidebar.setVisible(self.settings.get("ui.sidebar"))
            self._show_cursor(True)
        else:
            self._was_maximized = self.isMaximized()
            self.showFullScreen()
            if border_fix:
                winfix.set_border(int(self.winId()), True)
            self.sidebar.setVisible(bool(self.settings.get("ui.sidebar_in_fullscreen")))
            self._last_mouse_move = time.monotonic()
        self.controls.set_fullscreen(self.isFullScreen())

    def _fullscreen_autohide(self) -> None:
        if not self.isFullScreen():
            return
        position = QCursor.pos()
        if position != self._last_mouse:
            self._last_mouse = position
            self._last_mouse_move = time.monotonic()
            if not self.controls.isVisible():
                self.controls.setVisible(True)
            self._show_cursor(True)
            return
        idle_ms = (time.monotonic() - self._last_mouse_move) * 1000
        over_controls = self.controls.isVisible() and self.controls.rect().contains(self.controls.mapFromGlobal(position))
        if idle_ms > self.settings.get("ui.hide_delay_ms") and not over_controls and self.controls.isVisible():
            self.controls.setVisible(False)
            self._show_cursor(False)

    def _show_cursor(self, show: bool) -> None:
        if show and self._cursor_hidden:
            QApplication.restoreOverrideCursor()
            self._cursor_hidden = False
        elif not show and not self._cursor_hidden:
            QApplication.setOverrideCursor(Qt.CursorShape.BlankCursor)
            self._cursor_hidden = True

    def _follow_video_area(self) -> None:
        visible = self.isVisible() and not self.isMinimized() and self.stack.isVisible()
        if visible:
            top_left = self.stack.mapToGlobal(QPoint(0, 0))
            self.overlay.follow(QRect(top_left, self.stack.size()))
        if visible and not self.overlay.isVisible():
            self.overlay.show()
        elif not visible and self.overlay.isVisible():
            self.overlay.hide()

    # ================================================================ events
    def eventFilter(self, obj, event) -> bool:  # noqa: N802
        if event.type() == QEvent.Type.MouseButtonDblClick and self.player.has_media():
            if self.stack.rect().contains(self.stack.mapFromGlobal(QCursor.pos())) and self.isActiveWindow():
                self.toggle_fullscreen()
                return True
        if event.type() == QEvent.Type.Wheel and self.settings.get("playback.wheel_volume") and self.isActiveWindow():
            if self.stack.rect().contains(self.stack.mapFromGlobal(QCursor.pos())):
                steps = event.angleDelta().y() / 120.0
                if steps:
                    self.change_volume(int(round(steps * self.settings.get("playback.volume_step"))))
                    return True
        return super().eventFilter(obj, event)

    def dragEnterEvent(self, event) -> None:  # noqa: N802
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event) -> None:  # noqa: N802
        for url in event.mimeData().urls():
            path = Path(url.toLocalFile())
            if path.suffix.lower() in VIDEO_EXTENSIONS:
                self.open_film(path)
                return
            if path.suffix.lower() in (".srt", ".vtt", ".ass", ".ssa") and self.player.path:
                self.subtitles.load_external(path)
                return

    def _restore_geometry(self) -> None:
        geometry = self.settings.get("ui.window_geometry")
        if not (geometry and self.restoreGeometry(QByteArray.fromBase64(geometry.encode("ascii")))):
            self._first_geometry()
        splitter = self.settings.get("ui.splitter")
        if splitter:
            self.splitter.restoreState(QByteArray.fromBase64(splitter.encode("ascii")))

    def _first_geometry(self) -> None:
        """First start (nothing saved yet): 1280×760, or less on a smaller screen (a laptop at 150 %
        has only 1280×720 to share with the taskbar), in the middle of the screen."""
        screen = QGuiApplication.primaryScreen()
        if screen is None:
            return
        area = screen.availableGeometry()
        frame = 40   # the title bar and borders Windows adds around the window
        width = max(self.minimumSizeHint().width(), min(1280, int(area.width() * 0.94)))
        height = max(self.minimumSizeHint().height(), min(760, int(area.height() * 0.94) - frame))
        self.resize(width, height)
        self.move(area.x() + max(0, (area.width() - width) // 2),
                  area.y() + max(0, (area.height() - height - frame) // 2))

    def closeEvent(self, event) -> None:  # noqa: N802
        self._save_resume(force=True)
        if not self.isFullScreen():
            self.settings.set("ui.window_geometry", bytes(self.saveGeometry().toBase64()).decode("ascii"))
        self.settings.set("ui.splitter", bytes(self.splitter.saveState().toBase64()).decode("ascii"))
        self.settings.save()
        self.films.save_if_dirty()
        self.subtitles.stop()
        self.library.shutdown()
        self.controller.shutdown()
        self.player.shutdown()
        self._show_cursor(True)
        errors.set_handler(None)
        self.overlay.close()
        super().closeEvent(event)
