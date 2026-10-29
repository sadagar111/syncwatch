"""The sidebar: your room, who's watching, your friends, and what happened (activity feed).

Each panel can be hidden in Settings > Interface, and the whole sidebar with
the ☰ button or the Tab key. The panels only show things; the main window
decides what happens when you click (signals).
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from PySide6.QtCore import QPoint, Qt, QTimer, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QFrame, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMenu,
                               QPushButton, QSizePolicy, QToolButton, QVBoxLayout, QWidget)

from .. import __version__
from .qtutil import asset_pixmap, avatar_pixmap


class Panel(QFrame):
    def __init__(self, title: str, parent=None):
        super().__init__(parent)
        self.setObjectName("Panel")
        self.body = QVBoxLayout(self)
        self.body.setContentsMargins(10, 8, 10, 10)
        self.body.setSpacing(6)
        self.heading = QLabel(title.upper())
        self.heading.setObjectName("PanelTitle")
        self.body.addWidget(self.heading)


def _clear_layout(layout) -> None:
    while layout.count():
        item = layout.takeAt(0)
        if item.widget() is not None:
            item.widget().deleteLater()
        elif item.layout() is not None:
            _clear_layout(item.layout())


def _small_button(text: str, tip: str = "", accent: bool = False) -> QPushButton:
    button = QPushButton(text)
    if accent:
        button.setObjectName("Accent")
    if tip:
        button.setToolTip(tip)
    return button


# ============================================================================ your room
@dataclass
class WaitingEntry:
    key: bytes
    name: str
    short_id: str
    friend: bool
    friend_name: str = ""


class RoomPanel(Panel):
    hostClicked = Signal()
    joinClicked = Signal(str)
    leaveClicked = Signal()
    copyClicked = Signal()
    newCodeClicked = Signal()
    approveClicked = Signal(bytes)
    denyClicked = Signal(bytes, bool)        # (who, block too)
    approveAllClicked = Signal()

    def __init__(self, last_code: str = "", parent=None):
        super().__init__("Room", parent)
        self.status = QLabel("Not in a room")
        self.status.setWordWrap(True)
        self.body.addWidget(self.status)

        # not in a room
        self.host_button = _small_button("Host my room", "Open your room; friends join with its code.", accent=True)
        self.host_button.clicked.connect(self.hostClicked)
        self.join_title = QLabel("Join a room:")
        self.join_title.setObjectName("Dim")
        self.code_input = QLineEdit(last_code)
        self.code_input.setPlaceholderText("Paste a room code (SW-…)")
        self.code_input.returnPressed.connect(self._join)
        self.join_button = _small_button("Join")
        self.join_button.clicked.connect(self._join)
        join_row = QHBoxLayout()
        join_row.addWidget(self.code_input, 1)
        join_row.addWidget(self.join_button)
        self.idle_box = QWidget()
        idle = QVBoxLayout(self.idle_box)
        idle.setContentsMargins(0, 0, 0, 0)
        idle.addWidget(self.host_button, alignment=Qt.AlignmentFlag.AlignLeft)
        idle.addWidget(self.join_title)
        idle.addLayout(join_row)
        self.body.addWidget(self.idle_box)

        # hosting: the code
        self.code_box = QWidget()
        code_layout = QVBoxLayout(self.code_box)
        code_layout.setContentsMargins(0, 0, 0, 0)
        self.code_label = QLabel()
        self.code_label.setObjectName("Dim")
        self.code_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.code_label.setWordWrap(True)
        self.copy_button = _small_button("Copy code", "Copy the room code to send it to friends.", accent=True)
        self.copy_button.clicked.connect(self._copy)
        self.new_code_button = _small_button("New code…", "Make a new code. The old one stops working; people "
                                                          "in the room follow automatically.")
        self.new_code_button.clicked.connect(self.newCodeClicked)
        code_buttons = QHBoxLayout()
        code_buttons.addWidget(self.copy_button)
        code_buttons.addWidget(self.new_code_button)
        code_buttons.addStretch(1)
        code_layout.addWidget(self.code_label)
        code_layout.addLayout(code_buttons)
        self.body.addWidget(self.code_box)

        # waiting to be let in (host)
        self.waiting_box = QWidget()
        self.waiting_layout = QVBoxLayout(self.waiting_box)
        self.waiting_layout.setContentsMargins(0, 4, 0, 0)
        self.waiting_layout.setSpacing(4)
        self.body.addWidget(self.waiting_box)

        self.leave_button = _small_button("Leave")
        self.leave_button.clicked.connect(self.leaveClicked)
        leave_row = QHBoxLayout()
        leave_row.addWidget(self.leave_button)
        leave_row.addStretch(1)
        self.body.addLayout(leave_row)

        self._state: Optional[tuple] = None
        self._waiting_keys: Tuple = ()
        self.set_state("none")

    def _join(self) -> None:
        text = self.code_input.text().strip()
        if text:
            self.joinClicked.emit(text)

    def _copy(self) -> None:
        self.copyClicked.emit()
        self.copy_button.setText("Copied ✓")
        QTimer.singleShot(2500, lambda: self.copy_button.setText("Copy code"))

    def set_status(self, text: str, connected: bool) -> None:
        dot = "🟢" if connected else ("🟡" if text and not text.startswith("Not in") else "⚪")
        self.status.setText(f"{dot} {text}")

    def set_state(self, role: str, code: str = "", waiting: Optional[List[WaitingEntry]] = None) -> None:
        """role: none / host / joining / member"""
        state = (role, code)
        if state != self._state:
            self._state = state
            self.idle_box.setVisible(role == "none")
            self.code_box.setVisible(role == "host")
            self.leave_button.setVisible(role != "none")
            self.leave_button.setText({"host": "Close room", "joining": "Cancel"}.get(role, "Leave"))
            self.code_label.setText(f"Room code: {code}" if code else "")
            self.code_label.setToolTip(code)
        self._show_waiting((waiting or []) if role == "host" else [])

    def _show_waiting(self, waiting: List[WaitingEntry]) -> None:
        keys = tuple(w.key for w in waiting)
        if keys == self._waiting_keys:
            return
        self._waiting_keys = keys
        _clear_layout(self.waiting_layout)
        self.waiting_box.setVisible(bool(waiting))
        if not waiting:
            return
        title = QLabel("Waiting to join:")
        title.setObjectName("Warn")
        self.waiting_layout.addWidget(title)
        for entry in waiting:
            row = QHBoxLayout()
            tag = (f"friend{' “' + entry.friend_name + '”' if entry.friend_name and entry.friend_name != entry.name else ''}"
                   if entry.friend else "new person")
            label = QLabel(f"<b>{_html(entry.name)}</b> <span style='color:gray'>{entry.short_id} · {tag}</span>")
            label.setWordWrap(True)
            label.setToolTip("The ID is unique to each person's PC. Someone with a friend's name but "
                             "another ID is someone else.")
            yes = _small_button("Let in", accent=True)
            yes.clicked.connect(lambda _=False, k=entry.key: self.approveClicked.emit(k))
            no = QToolButton()
            no.setText("No ▾")
            no.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
            menu = QMenu(no)
            menu.addAction("Not now", lambda k=entry.key: self.denyClicked.emit(k, False))
            menu.addAction("Block this person", lambda k=entry.key: self.denyClicked.emit(k, True))
            no.setMenu(menu)
            row.addWidget(label, 1)
            row.addWidget(yes)
            row.addWidget(no)
            self.waiting_layout.addLayout(row)
        if len(waiting) > 1:
            everyone = _small_button(f"Let all {len(waiting)} in")
            everyone.clicked.connect(self.approveAllClicked)
            self.waiting_layout.addWidget(everyone, alignment=Qt.AlignmentFlag.AlignLeft)


def _html(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# ============================================================================ people
@dataclass
class PersonEntry:
    key: bytes                     # b"" for me
    name: str
    detail: str
    avatar: Optional[bytes]
    tooltip: str = ""
    actions: List[Tuple[str, str]] = field(default_factory=list)   # (label, action id)


class PersonRow(QWidget):
    menuRequested = Signal(object, QPoint)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.entry: Optional[PersonEntry] = None
        self.picture = QLabel()
        self.name = QLabel()
        self.name.setStyleSheet("font-weight: bold;")
        self.name.setTextFormat(Qt.TextFormat.PlainText)
        self.detail = QLabel()
        self.detail.setObjectName("Dim")
        self.detail.setWordWrap(True)
        text = QVBoxLayout()
        text.setSpacing(0)
        text.addWidget(self.name)
        text.addWidget(self.detail)
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 2, 0, 2)
        row.addWidget(self.picture)
        row.addLayout(text, 1)
        self._avatar_key: Optional[Tuple[int, int]] = None
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(lambda point: self.menuRequested.emit(self, self.mapToGlobal(point)))

    def set(self, entry: PersonEntry) -> None:
        self.entry = entry
        self.name.setText(entry.name)
        self.detail.setText(entry.detail)
        self.setToolTip(entry.tooltip + ("\nRight-click for more" if entry.actions else ""))
        key = (hash(entry.avatar) if entry.avatar else 0, 36)
        if key != self._avatar_key:
            self._avatar_key = key
            self.picture.setPixmap(avatar_pixmap(entry.avatar, 36))


class PeoplePanel(Panel):
    actionRequested = Signal(str, bytes)     # (action id, who)

    def __init__(self, parent=None):
        super().__init__("People", parent)
        self.rows_box = QVBoxLayout()
        self.rows_box.setSpacing(2)
        self.body.addLayout(self.rows_box)
        self.rows: Dict[bytes, PersonRow] = {}
        self._order: Tuple = ()

    def update_people(self, entries: List[PersonEntry]) -> None:
        order = tuple(e.key for e in entries)
        if order != self._order:
            self._order = order
            for row in self.rows.values():
                row.setParent(None)
                row.deleteLater()
            self.rows = {}
            for entry in entries:
                row = PersonRow()
                row.menuRequested.connect(self._menu)
                self.rows[entry.key] = row
                self.rows_box.addWidget(row)
        for entry in entries:
            self.rows[entry.key].set(entry)
        self.heading.setText(f"PEOPLE ({len(entries)})" if len(entries) > 1 else "PEOPLE")

    def _menu(self, row: PersonRow, point: QPoint) -> None:
        entry = row.entry
        if entry is None or not entry.actions:
            return
        menu = QMenu(self)
        for label, action in entry.actions:
            menu.addAction(label, lambda a=action, k=entry.key: self.actionRequested.emit(a, k))
        menu.exec(point)


# ============================================================================ friends
@dataclass
class FriendEntry:
    key: bytes
    name: str
    status: str
    avatar: Optional[bytes]
    can_join: bool = False
    request: bool = False          # an incoming friend request (Accept / Decline)
    short_id: str = ""


class FriendsPanel(Panel):
    joinClicked = Signal(bytes)
    acceptClicked = Signal(bytes)
    declineClicked = Signal(bytes)
    removeRequested = Signal(bytes)

    def __init__(self, parent=None):
        super().__init__("Friends", parent)
        self.rows_box = QVBoxLayout()
        self.rows_box.setSpacing(3)
        self.body.addLayout(self.rows_box)
        self.empty = QLabel("Add friends from the People list (right-click someone) when you're in a room "
                            "together. Then you see when they host and can join with one click.")
        self.empty.setObjectName("Dim")
        self.empty.setWordWrap(True)
        self.body.addWidget(self.empty)
        self._shown: Tuple = ()

    def update_friends(self, entries: List[FriendEntry]) -> None:
        shown = tuple((e.key, e.name, e.status, e.can_join, e.request, hash(e.avatar) if e.avatar else 0)
                      for e in entries)
        if shown == self._shown:
            return
        self._shown = shown
        _clear_layout(self.rows_box)
        self.empty.setVisible(not entries)
        for entry in entries:
            row = QHBoxLayout()
            picture = QLabel()
            picture.setPixmap(avatar_pixmap(entry.avatar, 28))
            text = QLabel(f"<b>{_html(entry.name)}</b><br><span style='color:gray'>{_html(entry.status)}</span>")
            text.setWordWrap(True)
            text.setToolTip(f"ID {entry.short_id}" if entry.short_id else "")
            row.addWidget(picture)
            row.addWidget(text, 1)
            if entry.request:
                accept = _small_button("Accept", accent=True)
                accept.clicked.connect(lambda _=False, k=entry.key: self.acceptClicked.emit(k))
                decline = _small_button("Decline")
                decline.clicked.connect(lambda _=False, k=entry.key: self.declineClicked.emit(k))
                row.addWidget(accept)
                row.addWidget(decline)
            else:
                if entry.can_join:
                    join = _small_button("Join", "Ask to join their room", accent=True)
                    join.clicked.connect(lambda _=False, k=entry.key: self.joinClicked.emit(k))
                    row.addWidget(join)
                more = QToolButton()
                more.setText("⋯")
                more.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
                menu = QMenu(more)
                menu.addAction("Remove friend…", lambda k=entry.key: self.removeRequested.emit(k))
                more.setMenu(menu)
                row.addWidget(more)
            self.rows_box.addLayout(row)


# ============================================================================ activity
class ActivityPanel(Panel):
    COLOURS = {"you": "#9BE8B4", "friend": "#9CC9FF", "warn": "#FFC46B", "error": "#FF8A9B", "info": None}

    def __init__(self, parent=None):
        super().__init__("Activity", parent)
        self.list = QListWidget()
        self.list.setWordWrap(True)
        self.list.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.list.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.body.addWidget(self.list, 1)
        self.max_items = 200

    def add(self, text: str, kind: str = "info") -> None:
        item = QListWidgetItem(f"{time.strftime('%H:%M')}  {text}")
        colour = self.COLOURS.get(kind)
        if colour:
            item.setForeground(QColor(colour))
        self.list.addItem(item)
        while self.list.count() > self.max_items:
            self.list.takeItem(0)
        self.list.scrollToBottom()


class UpdateBanner(QFrame):
    """Shown at the top of the sidebar when a new version is available or downloaded."""
    clicked = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("Panel")
        self.setStyleSheet("#Panel { border: 1px solid #F5A524; }")
        self.text = QLabel("")
        self.text.setWordWrap(True)
        self.button = QPushButton("What's new")
        self.button.setObjectName("Accent")
        self.button.clicked.connect(self.clicked)
        column = QVBoxLayout(self)
        column.setContentsMargins(10, 8, 10, 8)
        column.setSpacing(6)
        column.addWidget(self.text)
        button_row = QHBoxLayout()
        button_row.addStretch(1)
        button_row.addWidget(self.button)
        column.addLayout(button_row)
        self.setVisible(False)

    def set_version(self, version: str, ready: bool = False) -> None:
        if not version:
            self.setVisible(False)
            return
        if ready:
            self.text.setText(f"<b>Version {version} is downloaded.</b><br>Restart SyncWatch to start using it.")
            self.button.setText("Restart")
        else:
            self.text.setText(f"<b>Version {version} is available.</b>")
            self.button.setText("What's new")
        self.setVisible(True)
        # The sidebar sits in a splitter, which doesn't re-measure wrapped text by itself;
        # without this, going from one line to two cut off the second line.
        width = self.text.width() if self.text.width() > 50 else 250
        self.text.setMinimumHeight(self.text.heightForWidth(width))
        self.updateGeometry()


class Sidebar(QWidget):
    def __init__(self, last_code: str = "", parent=None):
        super().__init__(parent)
        self.setMinimumWidth(260)
        header = QHBoxLayout()
        logo = QLabel()
        pixmap = asset_pixmap("logo.png")
        if pixmap is not None:
            logo.setPixmap(pixmap.scaledToHeight(28, Qt.TransformationMode.SmoothTransformation))
        title = QLabel("<b>SyncWatch</b>")
        title.setStyleSheet("font-size: 13pt;")
        version = QLabel(f"v{__version__}")
        version.setObjectName("Hint")
        header.addWidget(logo)
        header.addWidget(title)
        header.addWidget(version)
        header.addStretch(1)
        self.update_banner = UpdateBanner()
        self.room = RoomPanel(last_code)
        self.people = PeoplePanel()
        self.friends = FriendsPanel()
        self.activity = ActivityPanel()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 8, 8, 8)
        layout.addLayout(header)
        layout.addWidget(self.update_banner)
        layout.addWidget(self.room)
        layout.addWidget(self.people)
        layout.addWidget(self.friends)
        layout.addWidget(self.activity, 1)

    def show_update(self, version: str, ready: bool = False) -> None:
        self.update_banner.set_version(version, ready)

    def apply_settings(self, settings) -> None:
        self.room.setVisible(settings.get("ui.panel_connection"))
        self.people.setVisible(settings.get("ui.panel_people"))
        self.friends.setVisible(settings.get("ui.panel_friends"))
        self.activity.setVisible(settings.get("ui.panel_activity"))
        self.activity.max_items = int(settings.get("ui.activity_max"))
