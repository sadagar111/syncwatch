"""The sidebar: connecting, who's watching, and what happened (activity feed).

Each panel can be hidden in Settings > Interface, and the whole sidebar with
the ☰ button or the Tab key.
"""
from __future__ import annotations

import time
from typing import List, Optional, Tuple

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QGuiApplication
from PySide6.QtWidgets import (QFrame, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem,
                               QPushButton, QSizePolicy, QToolButton, QVBoxLayout, QWidget)

from .. import __version__
from ..net.link import local_addresses
from .qtutil import asset_pixmap, avatar_pixmap


class Panel(QFrame):
    def __init__(self, title: str, parent=None):
        super().__init__(parent)
        self.setObjectName("Panel")
        self.body = QVBoxLayout(self)
        self.body.setContentsMargins(10, 8, 10, 10)
        self.body.setSpacing(6)
        heading = QLabel(title.upper())
        heading.setObjectName("PanelTitle")
        self.body.addWidget(heading)


class ConnectionPanel(Panel):
    hostClicked = Signal()
    leaveClicked = Signal()
    joinClicked = Signal(str)

    def __init__(self, last_address: str = "", parent=None):
        super().__init__("Watch together", parent)
        self.host_button = QPushButton("Host")
        self.host_button.setObjectName("Accent")
        self.host_button.setToolTip("Let a friend join you. Send them one of your addresses below.")
        self.host_button.clicked.connect(self.hostClicked)
        self.leave_button = QPushButton("Leave")
        self.leave_button.clicked.connect(self.leaveClicked)
        buttons = QHBoxLayout()
        buttons.addWidget(self.host_button)
        buttons.addWidget(self.leave_button)
        buttons.addStretch(1)
        self.body.addLayout(buttons)

        self.status = QLabel("Not connected")
        self.status.setWordWrap(True)
        self.body.addWidget(self.status)

        self.addresses_box = QVBoxLayout()
        self.addresses_box.setSpacing(3)
        self.body.addLayout(self.addresses_box)

        self.join_title = QLabel("Join a friend:")
        self.join_title.setObjectName("Dim")
        self.address = QLineEdit(last_address)
        self.address.setPlaceholderText("Friend's address, e.g. 100.101.102.103")
        self.address.returnPressed.connect(self._join)
        self.join_button = QPushButton("Join")
        self.join_button.clicked.connect(self._join)
        self._state = None
        join_row = QHBoxLayout()
        join_row.addWidget(self.address, 1)
        join_row.addWidget(self.join_button)
        self.body.addWidget(self.join_title)
        self.body.addLayout(join_row)
        self.set_state(False, False, False)

    def _join(self) -> None:
        text = self.address.text().strip()
        if text:
            self.joinClicked.emit(text)

    def set_state(self, connected: bool, hosting: bool, joining: bool) -> None:
        if (connected, hosting, joining) == self._state:
            return  # nothing changed: don't rebuild (keeps "Copied ✓" and avoids flicker)
        self._state = (connected, hosting, joining)
        busy = connected or hosting or joining
        self.host_button.setVisible(not busy)
        self.leave_button.setVisible(busy)
        self.leave_button.setText("Stop hosting" if hosting else "Leave")
        for widget in (self.join_title, self.address, self.join_button):
            widget.setVisible(not busy)
        self._show_addresses(hosting and not connected)

    def set_status(self, text: str, connected: bool) -> None:
        dot = "🟢" if connected else "⚪"
        self.status.setText(f"{dot} {text}")

    def _show_addresses(self, show: bool) -> None:
        while self.addresses_box.count():
            item = self.addresses_box.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
            elif item.layout() is not None:
                _clear_layout(item.layout())
        if not show:
            return
        title = QLabel("Send your friend one of these addresses:")
        title.setObjectName("Dim")
        title.setWordWrap(True)
        self.addresses_box.addWidget(title)
        addresses = local_addresses()
        if not addresses:
            none = QLabel("No network address found.")
            none.setObjectName("Warn")
            self.addresses_box.addWidget(none)
        for ip, label in addresses:
            row = QHBoxLayout()
            text = QLabel(f"<b>{ip}</b> <span style='color:gray'>({label})</span>")
            text.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            copy = QToolButton()
            copy.setText("Copy")
            copy.setToolTip(f"Copy {ip} to paste it to your friend")
            copy.clicked.connect(lambda _=False, a=ip, b=copy: (QGuiApplication.clipboard().setText(a),
                                                                 b.setText("Copied ✓")))
            row.addWidget(text, 1)
            row.addWidget(copy)
            self.addresses_box.addLayout(row)


def _clear_layout(layout) -> None:
    while layout.count():
        item = layout.takeAt(0)
        if item.widget() is not None:
            item.widget().deleteLater()
        elif item.layout() is not None:
            _clear_layout(item.layout())


class PersonRow(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.picture = QLabel()
        self.name = QLabel()
        self.name.setStyleSheet("font-weight: bold;")
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

    def set(self, name: str, detail: str, avatar: Optional[bytes]) -> None:
        self.name.setText(name)
        self.detail.setText(detail)
        key = (hash(avatar) if avatar else 0, 36)
        if key != self._avatar_key:
            self._avatar_key = key
            self.picture.setPixmap(avatar_pixmap(avatar, 36))


class PeoplePanel(Panel):
    def __init__(self, parent=None):
        super().__init__("People", parent)
        self.me = PersonRow()
        self.friend = PersonRow()
        self.body.addWidget(self.me)
        self.body.addWidget(self.friend)
        self.friend.setVisible(False)

    def update_people(self, me: Tuple[str, str, Optional[bytes]],
                      friend: Optional[Tuple[str, str, Optional[bytes]]]) -> None:
        self.me.set(*me)
        self.friend.setVisible(friend is not None)
        if friend is not None:
            self.friend.set(*friend)


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
    def __init__(self, last_address: str = "", parent=None):
        super().__init__(parent)
        self.setMinimumWidth(250)
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
        self.connection = ConnectionPanel(last_address)
        self.people = PeoplePanel()
        self.activity = ActivityPanel()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 8, 8, 8)
        layout.addLayout(header)
        layout.addWidget(self.update_banner)
        layout.addWidget(self.connection)
        layout.addWidget(self.people)
        layout.addWidget(self.activity, 1)

    def show_update(self, version: str, ready: bool = False) -> None:
        self.update_banner.set_version(version, ready)

    def apply_settings(self, settings) -> None:
        self.connection.setVisible(settings.get("ui.panel_connection"))
        self.people.setVisible(settings.get("ui.panel_people"))
        self.activity.setVisible(settings.get("ui.panel_activity"))
        self.activity.max_items = int(settings.get("ui.activity_max"))
