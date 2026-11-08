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
                               QPushButton, QScrollArea, QSizePolicy, QSplitter, QStackedWidget, QVBoxLayout,
                               QWidget)

from .. import errors, log as log_module, paths
from ..filmstate import film_key
from ..library import chapters as chapter_rules, episodes
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
from .sidebar import QueueEntry
from ..library import films as library_films
from ..units import human_size
from .torrent_ui import Torrents
from .youtube_ui import LinkBox, LinkDialog, TitleLookup, YouTube
from ..torrent import links as torrent_links
from .discover_ui import Discover, VotePanel
from .previews_ui import Previews
from ..youtube import links as youtube_links

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


def _short_time(ms: int) -> str:
    """'3:32' or '1:02:05' (fmt_time always shows hours)."""
    seconds = max(0, int(ms) // 1000)
    hours, minutes, rest = seconds // 3600, seconds % 3600 // 60, seconds % 60
    return f"{hours}:{minutes:02d}:{rest:02d}" if hours else f"{minutes}:{rest:02d}"


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
        youtube_card = QFrame()
        youtube_card.setObjectName("Card")
        youtube_title = QLabel("YouTube and torrents")
        youtube_title.setObjectName("LibraryTitle")
        youtube_note = QLabel("Paste a YouTube link, or a magnet link to watch a torrent while it downloads. "
                              "In a room, everyone who follows the host opens the same video.")
        youtube_note.setObjectName("Dim")
        youtube_note.setWordWrap(True)
        self.youtube_box = LinkBox()
        youtube_layout = QVBoxLayout(youtube_card)
        youtube_layout.setContentsMargins(18, 14, 18, 14)
        youtube_layout.setSpacing(6)
        youtube_layout.addWidget(youtube_title)
        youtube_layout.addWidget(youtube_note)
        youtube_layout.addWidget(self.youtube_box)
        self.torrent_file_button = QPushButton("📁  Open a .torrent file…")
        self.torrent_file_button.setToolTip("Watch a torrent while it downloads (the part you're watching first)")
        torrent_row = QHBoxLayout()
        torrent_row.addWidget(self.torrent_file_button)
        torrent_row.addStretch(1)
        youtube_layout.addLayout(torrent_row)
        ideas_card = QFrame()                      # what should we watch? (0.7)
        ideas_card.setObjectName("Card")
        ideas_title = QLabel("What should we watch?")
        ideas_title.setObjectName("LibraryTitle")
        ideas_note = QLabel("Ideas from the films you have and watched, trending films, trailers and where to "
                            "watch them. In a room: a film vote.")
        ideas_note.setObjectName("Dim")
        ideas_note.setWordWrap(True)
        self.ideas_button = QPushButton("💡  Ideas")
        self.ideas_button.setToolTip("Recommendations, trending films and your library, with posters (TMDB)")
        self.vote_button = QPushButton("🗳  Film vote")
        self.vote_button.setToolTip("Everyone in the room suggests films and votes (host or join a room first)")
        ideas_text = QVBoxLayout()
        ideas_text.setSpacing(4)
        ideas_text.addWidget(ideas_title)
        ideas_text.addWidget(ideas_note)
        ideas_layout = QHBoxLayout(ideas_card)
        ideas_layout.setContentsMargins(18, 14, 18, 14)
        ideas_layout.addLayout(ideas_text, 1)
        ideas_layout.addWidget(self.ideas_button)
        ideas_layout.addWidget(self.vote_button)
        self.hint = QLabel("Tip: drop a film file (or a YouTube link from your browser) here to play it straight "
                           "away, without adding it to the library.")
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
        column_layout.addWidget(youtube_card)
        column_layout.addWidget(ideas_card)
        column_layout.addWidget(self.hint)
        row = QHBoxLayout()   # the column as wide as it may be, the rest split evenly on both sides
        row.addStretch(1)
        row.addWidget(column, 30)
        row.addStretch(1)
        # The cards keep their natural height: on a short window the start screen scrolls instead of
        # squeezing them (squeezed, a card's text ran into its button).
        column.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        content = QWidget()
        content.setObjectName("StartContent")
        layout = QVBoxLayout(content)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.addStretch(2)
        layout.addLayout(row)
        layout.addStretch(3)   # a little above the middle looks centred to the eye
        scroll = QScrollArea()
        scroll.setObjectName("StartScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setStyleSheet("QScrollArea#StartScroll, QWidget#qt_scrollarea_viewport, QWidget#StartContent "
                             "{ background: transparent; }")
        scroll.setWidget(content)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(scroll)
        self._content_layout = layout
        self._scrollbar = scroll.verticalScrollBar()
        self._scrollbar.installEventFilter(self)

    def eventFilter(self, watched, event) -> bool:  # noqa: N802 (Qt naming)
        """While the scroll bar shows, the column moves over by as much: still in the middle of the window."""
        if watched is self._scrollbar and event.type() in (QEvent.Type.Show, QEvent.Type.Hide):
            extra = watched.width() if event.type() == QEvent.Type.Show else 0
            self._content_layout.setContentsMargins(24 + extra, 24, 24, 24)
        return super().eventFilter(watched, event)


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
        self._start_at_ms = 0          # a YouTube link's &t=: jump there once the video is open
        self._follow_align = False     # the video was opened because the host plays it: line up after
        self._queue_autostart = False  # the next video of the queue: start everyone together once it's open
        self._episode_autostart = False  # the next episode: the same, once it's open
        self._intro = None             # (end ms, title) of the intro playing now, if any
        self._intro_told = None        # the intro already hinted at (or skipped by itself)
        self._following_id = ""        # the host's video I'm opening (its problems only go to the activity feed)
        self._torrent_follow_tried: dict = {}   # magnet -> when following the host's torrent was last started
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
        self.youtube = YouTube(self.settings, self.ctx.profile.cache_dir, lambda: self.player.engine, self)
        self.youtube.opened.connect(self.play_stream)
        self.subtitles.fetch_youtube = self.youtube.fetch_captions
        self.subtitles.failed.connect(self.problem)
        self.youtube.busy.connect(self._youtube_busy)
        self.youtube.quietFailure.connect(self._youtube_follow_failed)
        self.youtube.playlistListed.connect(self._playlist_listed)
        self.youtube.failed.connect(self._youtube_failed)
        self.idle.youtube_box.submitted.connect(self.open_youtube)
        self.idle.youtube_box.queued.connect(self.queue_youtube)
        self.idle.youtube_box.magnetSubmitted.connect(self.open_torrent)
        self.idle.torrent_file_button.clicked.connect(self.pick_torrent_file)
        self.torrents = Torrents(self.settings, lambda: self.movies_dir() / "Torrents", self,
                                 cache=self.ctx.profile.cache_dir / "shared-films", library=self.movies_dir)
        self.torrents.shared.connect(lambda magnet, name, size: self.controller.share_ready(
            magnet, name, size, self.torrents.addresses))
        self.torrents.addressesChanged.connect(self._my_addresses_changed)
        self.sidebar.share_banner.clicked.connect(self._share_banner_clicked)
        self._torrent_offer: Optional[tuple] = None   # (magnet, name, size): the host's torrent, mine to stream too?
        self._getting_shared = ""      # the magnet of a film I'm getting from the host
        self.torrents.ready.connect(lambda source: self.play_stream(source, 0))
        self.torrents.statusText.connect(self.controls.set_source_text)
        self.torrents.message.connect(self.activity)
        self.idle.youtube_box.playlistSubmitted.connect(self.open_playlist)
        self.titles = TitleLookup(self)
        self.titles.found.connect(lambda video, title: self.controller.queue_info(video, title, 0))
        queue_panel = self.sidebar.queue
        queue_panel.playRequested.connect(self.controller.queue_play)
        queue_panel.removeRequested.connect(self.controller.queue_remove)
        queue_panel.moveRequested.connect(self.controller.queue_move)
        queue_panel.skipClicked.connect(self.controller.queue_vote_skip)   # (a plain skip when I may)
        queue_panel.addClicked.connect(lambda: self.ask_youtube_link(queue=True))
        self.discover = Discover(self)                     # what should we watch? the film vote (0.7)
        votes, vote_panel = self.controller.votes, self.sidebar.vote
        votes.have_check = self.discover.have_keys
        vote_panel.voteRequested.connect(votes.cast)
        vote_panel.withdrawRequested.connect(votes.withdraw)
        vote_panel.suggestClicked.connect(lambda: self.discover.open())
        vote_panel.closeClicked.connect(votes.close)
        vote_panel.cancelClicked.connect(votes.cancel)
        vote_panel.playClicked.connect(self.discover.play_winner)
        vote_panel.whereClicked.connect(self.discover.where_winner)
        self.idle.ideas_button.clicked.connect(lambda: self.discover.open())
        self.idle.vote_button.clicked.connect(self.start_film_vote)
        self.sidebar.activity.chatSubmitted.connect(self._send_chat)           # chat and reactions (0.8)
        self.sidebar.activity.chat_box.leave.connect(lambda: (self.sidebar.activity.chat_box.clearFocus(),
                                                              self.setFocus()))
        self.controls.reactionChosen.connect(lambda emoji: self.controller.chat.react(emoji))
        self.previews = Previews(self.settings, self.ctx.profile.cache_dir, self)   # (0.9)
        self.controls.seek.previews = self.previews
        self.player.ended.connect(self._media_ended)
        self.settings.subscribe("ui.panel_queue", lambda k, v: self.queue_changed())
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
        self.player.set_audio_delay(int(self.settings.get("playback.audio_delay_ms")))
        self.player.set_night_mode(bool(self.settings.get("playback.night_mode")))

        self.controls = ControlBar(theme.colors)
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(8, 8, 4, 8)
        left_layout.setSpacing(6)
        left_layout.addWidget(self.stack, 1)
        left_layout.addWidget(self.controls)

        self.sidebar = Sidebar(self.settings.get("room.last_code"))
        self.sidebar.vote = VotePanel()                    # the film vote, under "Up next" (0.7)
        self.sidebar.vote.hide()
        self.sidebar.layout().insertWidget(self.sidebar.layout().indexOf(self.sidebar.queue) + 1, self.sidebar.vote)
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
        ui.skipIntroClicked.connect(self.skip_intro)
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
        self.sidebar.people.readyClicked.connect(self._ready_clicked)
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
        self.settings.subscribe("playback.audio_delay_ms", lambda k, v: self.player.set_audio_delay(int(v)))
        self.settings.subscribe("playback.night_mode", lambda k, v: self.player.set_night_mode(bool(v)))

    def _apply_all_settings(self) -> None:
        self._apply_subtitle_style()
        self._install_shortcuts()
        self._apply_ui_settings()
        self._apply_volume()

    # ================================================================ the controller's UI interface
    def status(self, text: str, connected: bool) -> None:
        self.sidebar.room.set_status(text, connected)
        self.sidebar.activity.set_chat(connected)
        self._refresh_soon()
        if hasattr(self, "discover"):
            self.discover.vote_changed()

    # the film vote (0.7): the controller tells, the Discover part shows
    def vote_changed(self) -> None:
        if hasattr(self, "discover"):
            self.discover.vote_changed()

    def vote_won(self, choice) -> None:
        self.discover.vote_won(choice)

    def start_film_vote(self) -> None:
        if not self.controller.connected:
            self.activity("A film vote is for rooms: host a room or join one first", "info")
            return
        self.controller.votes.start()
        self.discover.open("you")

    def play_trailer(self, video_id: str) -> None:
        """A film's trailer: played here (in a room, for everyone following me), or, as a member following
        the host, in the browser (the host decides what plays in the room)."""
        c = self.controller
        if c.connected and not c.is_host and c.attached:
            from .discover_ui import open_link
            open_link(f"https://www.youtube.com/watch?v={video_id}")
            return
        self.open_youtube(youtube_links.VideoRef(video_id))

    # chat and reactions (0.8): the controller's RoomChat tells, these show
    def chat_message(self, name: str, text: str, mine: bool) -> None:
        self.sidebar.activity.add(f"💬 {'You' if mine else name}: {text}", "you" if mine else "friend")
        over = self.settings.get("chat.over_video")
        if not mine and (over == "always" or (over == "hidden" and not self.sidebar.isVisible())):
            self.overlay.toast(f"{name}: {text}", "friend")

    def reaction(self, name: str, emoji: str, mine: bool) -> None:
        if mine or self.settings.get("chat.reactions"):
            self.overlay.react(emoji, "" if mine else name)

    def _send_chat(self, text: str) -> None:
        if self.controller.chat.say(text):
            self.sidebar.activity.chat_box.clear()

    def focus_chat(self) -> None:
        if not self.controller.connected:
            return
        if not self.sidebar.isVisible():
            self.toggle_sidebar()
        self.sidebar.activity.chat_box.setFocus()

    def _react_key(self, number: int) -> None:
        from ..net.protocol import REACTIONS
        if self.settings.get("chat.number_keys") and self.controller.connected:
            self.controller.chat.react(REACTIONS[number - 1])

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
        if self.torrents.active and not (self.torrents.playing and path.name == self.torrents.playing.title):
            self.torrents.stop()
        if self.player.load(path):
            try:
                size = path.stat().st_size
            except OSError:
                size = 0
            self._film_key = film_key(path.name, size)
            self.previews.film_opened(path, self._film_key)        # pictures over the seek bar (0.9)
            self.setWindowTitle(f"SyncWatch: {path.name}")
            self.stack.setCurrentWidget(self.video)
            self.subtitles.film_opened(path, self._film_key)
            self.controller.set_film(path.name, size, 0)

    # ---------------------------------------------------------------- YouTube
    def open_youtube(self, ref) -> None:
        """Watch: find the video (in the background), then play it. In a room where I follow the
        host, only the host decides what plays for everyone, so it goes into the queue instead."""
        c = self.controller
        if c.connected and not c.is_host and c.attached:
            self.activity("In a room, the host decides what plays: added to the queue instead", "info")
            self.queue_youtube(ref)
            return
        self._follow_align = self._queue_autostart = False
        if self.youtube.open(ref):
            self.activity(f"Finding the YouTube video {ref.id}…", "you")

    def queue_youtube(self, ref) -> None:
        """Add to the queue (my own when I host or am alone; else a request to the host)."""
        if self.controller.queue_add(ref.id, "", 0) and self.controller.decides_queue:
            self.overlay.toast("Added to the queue", "info")

    def open_playlist(self, playlist, play: bool = True) -> None:
        """A playlist link: its video plays (if it named one) and the rest go into Up next
        (Settings > YouTube: ask / all / only the one video). Members add only the one video."""
        mode = self.settings.get("youtube.playlists")
        if mode == "video" or not self.controller.decides_queue:
            if playlist.video is not None:
                (self.open_youtube if play else self.queue_youtube)(playlist.video)
            if not self.controller.decides_queue and mode != "video":
                self.activity("Only the host adds whole playlists: send them the link", "info")
            return
        if playlist.video is not None and play:
            self.open_youtube(playlist.video)
        self._playlist_play = play and playlist.video is None
        self.youtube.list_playlist(playlist)

    def _playlist_listed(self, ref, playlist) -> None:
        from ..youtube.queue import MAX_ITEMS
        self.youtube.busy.emit("")
        entries = [entry for entry in playlist.entries if not (ref.video and entry.id == ref.video.id)]
        name = f"“{playlist.title}”" if playlist.title else "The playlist"
        if not entries:
            self.activity(f"{name} has no other videos SyncWatch can play", "info")
            return
        play_first = getattr(self, "_playlist_play", False)
        if self.settings.get("youtube.playlists") == "ask":
            count = len(entries) - (1 if play_first else 0)
            answer = QMessageBox.question(
                self, "Playlist", f"{name} has {len(entries)} video{'s' if len(entries) != 1 else ''}."
                + (f" Add {'the other ' if play_first else ''}{count} to Up next?" if count else ""),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            if answer != QMessageBox.StandardButton.Yes:
                if play_first:
                    self.open_youtube(youtube_links.VideoRef(entries[0].id))
                return
        if play_first:
            first = entries.pop(0)
            self.open_youtube(youtube_links.VideoRef(first.id))
        room = max(0, MAX_ITEMS - len(self.controller.queue.items))
        added = sum(1 for entry in entries[:room]
                    if self.controller.queue_add(entry.id, entry.title, int(entry.duration_s), quiet=True))
        left = len(entries) - room
        self.activity(f"Added {added} video{'s' if added != 1 else ''} from {name} to Up next"
                      + (f" ({left} didn't fit: the queue holds {MAX_ITEMS})" if left > 0 else ""), "you")

    def _open_or_queue(self, ref) -> None:
        """Ctrl+V or a dropped link: queue it if something from YouTube plays (or is queued), else play it."""
        youtube_on = getattr(self.player, "stream", None) is not None and self.player.has_media()
        if youtube_on or self.controller.queue.items:
            self.queue_youtube(ref)
        else:
            self.open_youtube(ref)

    def ask_youtube_link(self, queue: bool = False) -> None:
        """Ctrl+L: the link box on the start screen, or a small window while something plays."""
        if self.stack.currentWidget() is self.idle and not queue:
            self.idle.youtube_box.field.setFocus()
            return
        dialog = LinkDialog(self)
        if dialog.exec() and dialog.ref is not None:
            if dialog.mode.startswith("playlist"):
                self.open_playlist(dialog.ref, play=dialog.mode == "playlist" and not queue)
            elif dialog.mode == "queue" or queue:
                self.queue_youtube(dialog.ref)
            else:
                self.open_youtube(dialog.ref)

    # the controller: the queue changed / the next video starts
    def queue_changed(self) -> None:
        c, queue = self.controller, self.controller.queue
        entries = [QueueEntry(item.key, item.title, _short_time(item.dur) if item.dur else "",
                              item.by, item.by == c.name) for item in queue.items]
        now = f"▶ Now: {queue.now.title}" if queue.now is not None else ""
        panel = self.sidebar.queue
        vote = ""
        if c.can_vote_skip():
            votes = queue.skip_votes()
            vote = f"⏭ Vote to skip ({votes[0]}/{votes[1]})" if votes else "⏭ Vote to skip"
            if c.voted_to_skip():
                vote = vote.replace("Vote to skip", "Voted")
        panel.update_queue(now, entries, c.can_queue("add"), c.can_queue("control"), vote)
        panel.setVisible(bool(self.settings.get("ui.panel_queue")) and bool(queue.items or queue.now))
        if queue.next is not None:
            self.youtube.prefetch(queue.next.id)    # the next video starts without waiting for YouTube
        if c.decides_queue:
            for item in queue.items:
                if item.title == item.id:
                    self.titles.ask(item.id)

    def play_queue_item(self, item) -> None:
        self._follow_align = False
        self._queue_autostart = True
        self.activity(f"Next: {item.title}", "you")
        # quiet: a queued video that's gone is skipped with a line in the activity feed, no popup
        if not self.youtube.open(youtube_links.VideoRef(item.id), start_s=0, quiet=True):
            self._queue_autostart = False

    def _media_ended(self) -> None:
        """A video ended: the next one in the queue starts (host or alone, if Play the next is on).
        An episode of a show: the next episode (Settings > Playback)."""
        if getattr(self.player, "stream", None) is None:
            ended = self.player.path
            if ended is not None and self.settings.get("playback.next_episode") \
                    and (self.controller.is_host or not self.controller.connected) and episodes.next_after(ended):
                self.overlay.toast("Next episode in 3 s (N: now)", "info")
                QTimer.singleShot(3000, lambda: self.player.path == ended and self.next_episode())
            return
        if not self.settings.get("youtube.autoplay"):
            return
        if self.controller.decides_queue and self.controller.queue.items:
            self.controller.queue_next()

    # ---------------------------------------------------------------- episodes
    def next_episode(self) -> None:
        """Key N, or an episode's end: the show's next episode (host or alone); everyone starts it together."""
        current = self.player.path if getattr(self.player, "stream", None) is None else None
        following = self.controller.connected and not self.controller.is_host and self.controller.attached
        if following:
            self.overlay.toast("In a room, the host goes on to the next episode", "info")
            return
        path = episodes.next_after(current) if current is not None else None
        if path is None:
            self.overlay.toast("No next episode next to this one", "info")
            return
        episode = episodes.info(path.name)
        self.activity(f"Next episode: {episode.label if episode else path.name}", "you")
        self._episode_autostart = True
        self.open_film(path)

    def follow_film(self, name: str, size: int) -> None:
        """The controller: the host opened another film. If it's another episode of the show I'm
        watching and I have it next to mine, mine opens and lines up (Settings > Watching together)."""
        current = self.player.path if getattr(self.player, "stream", None) is None else None
        wanted, mine = episodes.info(name), (episodes.info(current.name) if current is not None else None)
        if not self.settings.get("sync.follow_episodes") or wanted is None or mine is None \
                or not wanted.same_show(mine) or wanted == mine:
            return
        path = episodes.find(wanted, current)
        if path is None:
            self.activity(f"The host went on to {wanted.label}, but it isn't next to your {mine.label}", "warn")
            return
        self.activity(f"The host went on to {wanted.label}: opening yours ({path.name})", "friend")
        self._follow_align = True
        self.open_film(path)

    def follow_youtube(self, video_id: str, title: str = "") -> None:
        """The controller: the host plays a YouTube video I don't have open."""
        if not self.settings.get("youtube.follow_host") or self.youtube.busy_with() == video_id \
                or self.youtube.failed_recently(video_id):
            return
        if self.youtube.open(youtube_links.VideoRef(video_id), start_s=0, quiet=True):
            self._follow_align = True
            self._following_id = video_id
            self.activity(f"The host plays a YouTube video: opening {title or video_id}", "friend")

    def follow_torrent(self, magnet: str, name: str, size: int) -> None:
        """The controller: the host streams a torrent I don't have open. A film of that name and size
        in my library is opened instead (no download needed). Otherwise the sidebar asks first
        (streaming a torrent also uploads it to others), unless Settings > Torrents says otherwise."""
        choice = self.settings.get("torrents.follow_host")
        if choice == "no" or not torrent_links.is_magnet(magnet):
            return
        if self.torrents.active and self.torrents.hash and self.torrents.hash == torrent_links.magnet_hash(magnet):
            return   # already streaming it
        if time.monotonic() - self._torrent_follow_tried.get(magnet, -1e9) < 60:
            return   # tried a moment ago (e.g. it failed): don't start again on every heartbeat
        self._torrent_follow_tried[magnet] = time.monotonic()
        known = library_films.list_films(self.movies_dir()) + library_films.list_films(self.torrents.folder())
        local = next((film.path for film in known if film.path.name == name and film.size == size), None)
        if local is not None:
            self._follow_align = True
            self.activity(f"The host streams {name}: you have it in your library, opening that", "friend")
            self.open_film(local)
            return
        if choice == "ask":
            if self._torrent_offer is None or self._torrent_offer[0] != magnet:
                self._torrent_offer = (magnet, name, int(size))
                self.activity(f"The host streams {name or 'a film'} from a torrent. Stream it too? (sidebar)",
                              "friend")
            self._refresh_share_banner()
            return
        self._stream_hosts_torrent(magnet, name)

    def _stream_hosts_torrent(self, magnet: str, name: str, clicked: bool = False) -> None:
        self._torrent_offer = None
        self._follow_align = True
        if not clicked:   # (after a click, the torrent's own "Starting the torrent…" line says enough)
            self.activity(f"The host streams {name or 'a torrent'}: streaming it too", "friend")
        self.torrents.open(magnet)

    def _torrent_offer_still_open(self) -> bool:
        """The host still streams the torrent offered in the sidebar, and I don't (yet)."""
        offer, c = self._torrent_offer, self.controller
        host = c.host_peer
        if offer is None or host is None or c.is_host or not c.attached or not host.film:
            return False
        if host.film.get("magnet") != offer[0]:
            return False
        return not (self.torrents.active and self.torrents.hash == torrent_links.magnet_hash(offer[0]))

    def play_stream(self, stream, start_s: int = 0) -> None:
        """A YouTube video was found: play it like a film."""
        self._save_resume(force=True)
        if stream.kind != "torrent" and self.torrents.active:
            self.torrents.stop()            # something else plays now
        if not self.player.load_stream(stream):
            return
        self._film_key = film_key(stream.title, stream.size) if stream.kind == "torrent" else f"yt:{stream.id}"
        self.previews.clear()
        self._start_at_ms = max(0, int(start_s)) * 1000
        self.setWindowTitle(f"SyncWatch: {stream.label}")
        self.stack.setCurrentWidget(self.video)
        if stream.kind == "torrent":
            self.subtitles.film_closed()
            self.controller.set_film(stream.title, stream.size, 0, magnet=self.torrents.magnet())
        else:
            self.subtitles.stream_opened(self._film_key, stream.id, stream.captions)
            self.controller.set_film(stream.label, 0, stream.duration_ms, yt=stream.id)

    def _youtube_busy(self, text: str) -> None:
        if text:
            self.overlay.toast(text, "info")

    def _youtube_failed(self, video_id: str, code: str) -> None:
        """A queued video can't be opened (removed, private…): go on with the next one."""
        now = self.controller.queue.now
        if self._queue_autostart and now is not None and now.id == video_id:
            self._queue_autostart = False
            self.activity(f"Skipping {now.title}: {errors.CATALOG[code].title if code in errors.CATALOG else code}",
                          "warn")
            if self.controller.decides_queue and self.controller.queue.items:
                QTimer.singleShot(500, self.controller.queue_next)

    def _youtube_follow_failed(self, video_id: str, code: str) -> None:
        if video_id != self._following_id:
            return   # a queued video: _youtube_failed already said so
        self._following_id = ""
        problem = errors.CATALOG.get(code)
        title = problem.title if problem else code
        self.activity(f"Couldn't open the host's YouTube video {video_id}: {title} ({code})", "warn")

    # ---------------------------------------------------------------- torrents (0.6)
    def open_torrent(self, source: str) -> None:
        """A magnet link or a .torrent file: stream it (the part being watched first)."""
        self.torrents.open(source)

    # sharing a film with the room
    def _refresh_share_banner(self) -> None:
        self._my_copy = None
        if self._offer_torrent():
            return
        film = self.controller.can_ask_for_film()
        host = self.controller.host_peer
        if film is None or host is None or self._getting_shared or self.torrents.active:
            self.sidebar.share_banner.offer("", "")
            return
        from ..discover.names import find_copy
        from ..library import films as library_files
        copy = find_copy(((f.name, f.size, f.path) for f in library_files.list_films(self.library.folder())),
                         film["name"], int(film["size"]))
        if copy is not None and copy != self.player.path:     # it's in my library, maybe under another name
            self._my_copy = copy
            self.sidebar.share_banner.offer(
                f"{host.name} is watching {film['name']}. You have it in your library: {copy.name}.", "Open my copy")
            return
        self.sidebar.share_banner.offer(
            f"{host.name} is watching {film['name']} ({human_size(film['size'])}). You don't have it: get it "
            f"straight from {host.name}? It plays while it downloads.", f"Get it from {host.name}")

    def _offer_torrent(self) -> bool:
        """The sidebar asks whether to stream the host's torrent too (Settings > Torrents: ask)."""
        if not self._torrent_offer_still_open():
            self._torrent_offer = None
            return False
        _, name, size = self._torrent_offer
        host = self.controller.host_peer
        about = f"{name} ({human_size(size)})" if size else (name or "a film")
        self.sidebar.share_banner.offer(
            f"{host.name} streams {about} from a torrent. Stream it too? Your PC then downloads it and "
            "shares the parts it has with others, like any torrent app.", "Stream it too")
        return True

    def _share_banner_clicked(self) -> None:
        if self._torrent_offer_still_open():
            magnet, name, _ = self._torrent_offer
            self.sidebar.share_banner.offer("", "")
            self._stream_hosts_torrent(magnet, name, clicked=True)
            return
        copy = getattr(self, "_my_copy", None)
        if copy is not None and copy.is_file():
            self.sidebar.share_banner.offer("", "")
            self.open_film(copy)
        else:
            self._ask_for_film()

    def _ask_for_film(self) -> None:
        if self.controller.ask_for_film(self.torrents.addresses):
            self.sidebar.share_banner.offer("", "")

    def share_requested(self, who: str, their_addresses: list) -> None:
        """The controller (I host): someone in the room asks for my film."""
        path = self.player.path
        if path is None or not path.is_file():
            return
        answer = self.settings.get("torrents.share")
        if answer == "no":
            self.activity(f"{who} asked for {path.name} (sharing is off: Settings > Torrents)", "info")
            return
        if answer == "ask":
            reply = QMessageBox.question(
                self, "Share your film?",
                f"{who} doesn't have {path.name}. Send it to them straight from your PC? It plays for them "
                "while it downloads. Nothing goes through the relay.")
            if reply != QMessageBox.StandardButton.Yes:
                return
        if not (self.torrents.active and self.torrents.mode == "share" and self.torrents.name == path.name):
            self.torrents.share(path)
        elif self.torrents._share_info is not None:   # already shared: tell the room again
            self.controller.share_ready(*self.torrents._share_info, self.torrents.addresses)
        self.share_peer(who, their_addresses)

    def share_peer(self, who: str, addresses: list) -> None:
        """Where someone who fetches my film can be reached: connect to them too (another way in)."""
        if self.torrents.active and self.torrents.mode == "share":
            for address in addresses:
                if torrent_links.valid_address(address):
                    self.torrents.add_peer(address)

    def receive_share(self, magnet: str, name: str, size: int, addresses: list) -> None:
        """The controller: the host shares the film I asked for. Stream it, from the host."""
        if not torrent_links.is_magnet(magnet):
            return
        if self._getting_shared != magnet or not self.torrents.active:
            self._getting_shared = magnet
            self._follow_align = True
            self.activity(f"Getting {name} ({human_size(size)}) from the host…", "friend")
            self.torrents.open(magnet, private=True)
        for address in addresses:
            if torrent_links.valid_address(address):
                self.torrents.add_peer(address)

    def _my_addresses_changed(self, addresses: list) -> None:
        if self._getting_shared and self.torrents.mode == "stream":
            self.controller.share_addresses(addresses)

    def pick_torrent_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Open a torrent file", str(Path.home() / "Downloads"),
                                              "Torrent files (*.torrent)")
        if path:
            self.open_torrent(path)

    def _paste_link(self) -> None:
        """Ctrl+V outside a text field: a YouTube link from the clipboard opens."""
        text = QApplication.clipboard().text()
        magnet = torrent_links.find_magnet(text)
        if magnet is not None:
            self.open_torrent(magnet)
            return
        playlist = youtube_links.find_playlist(text)
        if playlist is not None:
            youtube_on = getattr(self.player, "stream", None) is not None and self.player.has_media()
            self.open_playlist(playlist, play=not (youtube_on or self.controller.queue.items))
            return
        ref = youtube_links.find(text)
        if ref is not None:
            self._open_or_queue(ref)

    def close_film(self) -> None:
        """Close the open film (e.g. before it's removed from the library): back to the start screen."""
        self._save_resume(force=True)
        self.player.unload()
        self.torrents.stop()
        self.previews.clear()
        self.subtitles.film_closed()
        self._film_key = ""
        self.setWindowTitle("SyncWatch")
        self.stack.setCurrentWidget(self.idle)
        self.controller.set_film("", 0, 0)
        self._refresh_soon()

    def _film_loaded(self) -> None:
        path, stream = self.player.path, getattr(self.player, "stream", None)
        if path is None and stream is None:
            return
        duration = self.player.duration()
        memory = self.films.get(self._film_key)
        resume = int(memory.get("pos_ms", 0))
        minimum = 1000 * int(self.settings.get("playback.resume_min_s"))
        start, self._start_at_ms = self._start_at_ms, 0
        if start and start < duration:
            self.controller.seek(start)   # the link's &t= (in a room: for everyone, if allowed)
            self.activity(f"Starting at {fmt_time(start)}, as the link says", "you")
        elif (self.settings.get("playback.resume") and not self.controller.connected
                and minimum < resume < duration - 60_000):
            self.player.seek(resume)
            self.activity(f"Continuing where you stopped ({fmt_time(resume)})", "you")
        if self._follow_align:
            self._follow_align = False
            self.controller.align_on_next_state()
        self._apply_audio_preference()
        if stream is not None and stream.kind == "torrent":
            self.controller.set_film(stream.title, stream.size, duration, magnet=self.torrents.magnet())
            if self._follow_align:
                self._follow_align = False
                self.controller.align_on_next_state()
            self.controller.queue_playing_elsewhere()
            self.activity(f"Opened {stream.title} ({fmt_time(duration)}), streaming from the torrent", "you")
        elif stream is not None:
            self.controller.set_film(stream.label, 0, duration or stream.duration_ms, yt=stream.id)
            self.controller.queue_info(stream.id, stream.label, duration or stream.duration_ms)
            if self._queue_autostart:
                self._queue_autostart = False
                self.controller.start_together()
            else:
                self.controller.queue_now_playing(stream.id, stream.label, duration or stream.duration_ms)
            details = ", ".join(part for part in ("YouTube", stream.describe(), fmt_time(duration)) if part)
            self.activity(f"Opened {stream.label} ({details})", "you")
        else:
            self.controller.set_film(path.name, path.stat().st_size if path.exists() else 0, duration)
            self.controller.queue_playing_elsewhere()
            self.activity(f"Opened {path.name} ({fmt_time(duration)})", "you")
            if self._episode_autostart:
                self._episode_autostart = False
                self.controller.start_together()      # (alone: it just plays)
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
        self._check_intro(position, duration)
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
            elif peer.match == "elsewhere":
                state += " · watching something else"
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
        self._refresh_share_banner()
        self.sidebar.people.update_ready(*self._ready_texts())
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
            none = menu.addAction("YouTube has no subtitles for this video" if subs.video_id
                                  else "No subtitles found next to the film")
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
            "keys.ready": self._ready_clicked,
            "keys.next_episode": self.next_episode,
            "keys.night_mode": self._toggle_night_mode,
            "keys.skip_intro": self.skip_intro,
            "keys.subs_toggle": lambda: self.settings.set("subs.enabled", not self.settings.get("subs.enabled")),
            "keys.subs_earlier": lambda: self._nudge_subs(-1),
            "keys.subs_later": lambda: self._nudge_subs(1),
            "keys.audio_earlier": lambda: self._nudge_sound(-1),
            "keys.audio_later": lambda: self._nudge_sound(1),
            "keys.sidebar": self.toggle_sidebar,
            "keys.open": self.open_library,
            "keys.youtube": self.ask_youtube_link,
            "keys.chat": self.focus_chat,
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
        paste = QShortcut(QKeySequence(QKeySequence.StandardKey.Paste), self)   # text fields keep their own
        paste.setContext(Qt.ShortcutContext.WindowShortcut)
        paste.activated.connect(self._paste_link)
        self._shortcuts.append(paste)
        for number in range(1, 9):        # reactions (0.8); text fields keep their digits
            react = QShortcut(QKeySequence(str(number)), self)
            react.setContext(Qt.ShortcutContext.WindowShortcut)
            react.activated.connect(lambda n=number: self._react_key(n))
            self._shortcuts.append(react)
        escape = QShortcut(QKeySequence("Esc"), self)
        escape.activated.connect(self._escape)
        self._shortcuts.append(escape)

    def _ready_clicked(self) -> None:
        c = self.controller
        if c.connected and c.is_host:
            c.ready_check()
        else:
            c.i_am_ready()

    def _ready_texts(self):
        """(line, button) for the ready check in the People panel."""
        c, view = self.controller, self.controller.ready_view
        if not c.connected:
            return "", ""
        names = ", ".join("you" if name == c.name else name for name in (view or {}).get("waiting", []))
        if c.is_host:
            if view is not None:
                return f"Ready check: waiting for {names}", "Call off the ready check"
            return "", ("✋ Ready check" if any(not p.separate for p in c.peers.values()) else "")
        if view is None:
            return "", ""
        if view.get("me"):
            return f"You're ready ✓ · the host waits for {names}" if names else "You're ready ✓", ""
        return f"The host asks: everyone ready? Waiting for {names}", "✓ I'm ready"

    def _nudge_subs(self, direction: int) -> None:
        step = int(self.settings.get("subs.delay_step_ms"))
        self.subtitles.nudge(direction * step)
        self.overlay.toast(f"Subtitle delay {self.subtitles.delay_ms / 1000:+.2f} s", "info")

    def _nudge_sound(self, direction: int) -> None:
        if self.player.engine != "mpv":
            self.overlay.toast("Moving the sound needs mpv (Settings > Playback > Video player)", "info")
            return
        delay = max(-2000, min(2000, int(self.settings.get("playback.audio_delay_ms")) + 25 * direction))
        self.settings.set("playback.audio_delay_ms", delay)
        self.overlay.toast(f"Sound delay {delay / 1000:+.3f} s" + ("  (earlier than the picture)" if delay < 0 else ""),
                           "info")

    def _toggle_night_mode(self) -> None:
        if self.player.engine != "mpv":
            self.overlay.toast("Night mode needs mpv (Settings > Playback > Video player)", "info")
            return
        on = not self.settings.get("playback.night_mode")
        self.settings.set("playback.night_mode", on)
        self.overlay.toast("Night mode on: loud and quiet evened out" if on else "Night mode off", "info")

    # ---------------------------------------------------------------- intros
    def _check_intro(self, position: float, duration: int) -> None:
        """Inside an intro, opening or recap chapter: Skip intro shows (or it's skipped, Settings > Playback)."""
        mode = self.settings.get("playback.skip_intros")
        found = None
        if mode != "off" and self.player.is_playing():
            found = chapter_rules.skippable_at(self.player.chapters(), position, duration)
        self._intro = found
        self.controls.skip_intro.setVisible(found is not None)
        if found is None or found == self._intro_told:
            return
        self._intro_told = found
        c = self.controller
        if mode == "auto" and (c.is_host or not c.connected):
            self.skip_intro()
        else:
            self.overlay.toast(f"{found[1]}: press I to skip it", "info")

    def skip_intro(self) -> None:
        if self._intro is None:
            return
        end, title = self._intro
        self._intro = None
        self.controls.skip_intro.hide()
        self.activity(f"Skipped the {title.lower()}", "you")
        self.controller.seek(end)

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
        lines += ["", "In a room: 1–8 send reactions (😂 😮 😍 😱 😢 👏 🔥 🍿)",
                  "Double-click the video: fullscreen · Esc: close this"]
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
        data = event.mimeData()
        if data.hasUrls() or (data.hasText() and youtube_links.find(data.text())):
            event.acceptProposedAction()

    def dropEvent(self, event) -> None:  # noqa: N802
        data = event.mimeData()
        for url in data.urls():
            if not url.isLocalFile():
                playlist = youtube_links.parse_playlist(url.toString())
                if playlist is not None:
                    self.open_playlist(playlist)
                    return
                ref = youtube_links.parse(url.toString())
                if ref is not None:
                    self._open_or_queue(ref)
                    return
                continue
            path = Path(url.toLocalFile())
            if path.suffix.lower() in VIDEO_EXTENSIONS:
                self.open_film(path)
                return
            if torrent_links.is_torrent_file(path):
                self.open_torrent(str(path))
                return
            if path.suffix.lower() in (".srt", ".vtt", ".ass", ".ssa") and self.player.path:
                self.subtitles.load_external(path)
                return
        magnet = torrent_links.find_magnet(data.text()) if data.hasText() else None
        if magnet is not None:
            self.open_torrent(magnet)
            return
        ref = youtube_links.find(data.text()) if data.hasText() else None
        if ref is not None:   # a link dragged as text
            self._open_or_queue(ref)

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
        self.youtube.stop()
        self.torrents.stop()
        self.discover.shutdown()
        self.previews.stop()
        self.library.shutdown()
        self.controller.shutdown()
        self.player.shutdown()
        self._show_cursor(True)
        errors.set_handler(None)
        self.overlay.close()
        super().closeEvent(event)
