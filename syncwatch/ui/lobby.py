"""The lobby (0.5.2): what SyncWatch shows when it starts.

Four cards: host a room, join one (with the code box right there), watch alone, settings. Friends
stay in the sidebar on the right, with Join next to anyone who hosts. Host, Join and Watch alone
lead to the start screen (your films, links, Ideas). The 🏠 button in the control bar, or leaving
the room, comes back here.
"""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QFrame, QGridLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton, QScrollArea,
                               QSizePolicy, QVBoxLayout, QWidget)

from .qtutil import asset_pixmap


def _card(title: str, text: str) -> tuple:
    """A card like the start screen's: (frame, its layout) with a title and a short text."""
    frame = QFrame()
    frame.setObjectName("Card")
    layout = QVBoxLayout(frame)
    layout.setContentsMargins(18, 16, 18, 16)
    layout.setSpacing(8)
    heading = QLabel(title)
    heading.setObjectName("LibraryTitle")
    note = QLabel(text)
    note.setObjectName("Dim")
    note.setWordWrap(True)
    layout.addWidget(heading)
    layout.addWidget(note)
    layout.addStretch(1)
    return frame, layout


class LobbyScreen(QWidget):
    hostClicked = Signal()
    joinClicked = Signal(str)
    aloneClicked = Signal()
    settingsClicked = Signal()
    COLUMN_WIDTH = 720

    def __init__(self, last_code: str = "", parent=None):
        super().__init__(parent)
        logo = QLabel()
        pixmap = asset_pixmap("logo.png")
        if pixmap is not None:
            ratio = self.devicePixelRatioF()
            scaled = pixmap.scaled(round(72 * ratio), round(72 * ratio), Qt.AspectRatioMode.KeepAspectRatio,
                                   Qt.TransformationMode.SmoothTransformation)
            scaled.setDevicePixelRatio(ratio)
            logo.setPixmap(scaled)
        logo.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title = QLabel("SyncWatch")
        title.setObjectName("LobbyTitle")
        title.setStyleSheet("font-size: 22pt; font-weight: 600;")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.greeting = QLabel("Watch films and videos together, in sync.")
        self.greeting.setObjectName("Dim")
        self.greeting.setTextFormat(Qt.TextFormat.PlainText)      # (it holds your name)
        self.greeting.setAlignment(Qt.AlignmentFlag.AlignCenter)

        host, host_layout = _card("🎬  Host a room", "Open your room and pick what to watch. Friends join with its "
                                                     "code, or with one click from their Friends list.")
        self.host_button = QPushButton("Host a room")
        self.host_button.setObjectName("Accent")
        self.host_button.clicked.connect(self.hostClicked)
        host_layout.addWidget(self.host_button, alignment=Qt.AlignmentFlag.AlignLeft)

        join, join_layout = _card("🔗  Join a room", "Paste the room code a friend sent you.")
        self.code_input = QLineEdit(last_code)
        self.code_input.setPlaceholderText("Room code (SW-…)")
        self.code_input.returnPressed.connect(self._join)
        self.join_button = QPushButton("Join")
        self.join_button.clicked.connect(self._join)
        join_row = QHBoxLayout()
        join_row.addWidget(self.code_input, 1)
        join_row.addWidget(self.join_button)
        join_layout.addLayout(join_row)

        alone, alone_layout = _card("🍿  Watch alone", "Your films, a link or a YouTube video, just for you. "
                                                       "You can still open a room later.")
        self.alone_button = QPushButton("Watch alone")
        self.alone_button.clicked.connect(self.aloneClicked)
        alone_layout.addWidget(self.alone_button, alignment=Qt.AlignmentFlag.AlignLeft)

        options, options_layout = _card("⚙  Settings", "Your name and picture, playback, subtitles, rooms, "
                                                      "downloads and more.")
        self.settings_button = QPushButton("Settings")
        self.settings_button.clicked.connect(self.settingsClicked)
        options_layout.addWidget(self.settings_button, alignment=Qt.AlignmentFlag.AlignLeft)

        grid = QGridLayout()
        grid.setHorizontalSpacing(14)
        grid.setVerticalSpacing(14)
        for index, card in enumerate((host, join, alone, options)):
            grid.addWidget(card, index // 2, index % 2)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)
        self.cards = (host, join, alone, options)

        self.hint = QLabel("Your friends are on the right. When one of them hosts, Join appears next to their name.")
        self.hint.setObjectName("Hint")
        self.hint.setWordWrap(True)
        self.hint.setAlignment(Qt.AlignmentFlag.AlignCenter)

        column = QWidget()
        column.setMaximumWidth(self.COLUMN_WIDTH)
        column.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        column_layout = QVBoxLayout(column)
        column_layout.setContentsMargins(0, 0, 0, 0)
        column_layout.setSpacing(10)
        column_layout.addWidget(logo)
        column_layout.addWidget(title)
        column_layout.addWidget(self.greeting)
        column_layout.addSpacing(14)
        column_layout.addLayout(grid)
        column_layout.addSpacing(6)
        column_layout.addWidget(self.hint)
        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(column, 30)
        row.addStretch(1)
        content = QWidget()
        content.setObjectName("LobbyContent")
        layout = QVBoxLayout(content)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.addStretch(2)
        layout.addLayout(row)
        layout.addStretch(3)       # a little above the middle looks centred to the eye
        scroll = QScrollArea()
        scroll.setObjectName("LobbyScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setStyleSheet("QScrollArea#LobbyScroll, QWidget#qt_scrollarea_viewport, QWidget#LobbyContent "
                             "{ background: transparent; }")
        scroll.setWidget(content)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(scroll)

    def set_name(self, name: str) -> None:
        name = " ".join((name or "").split())[:40]
        self.greeting.setText(f"Hi {name}! Watch films and videos together, in sync." if name
                              else "Watch films and videos together, in sync.")

    def _join(self) -> None:
        text = self.code_input.text().strip()
        if text:
            self.joinClicked.emit(text)
        else:
            self.code_input.setFocus()
