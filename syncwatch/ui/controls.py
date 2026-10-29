"""The control bar under the video: seek bar, playback buttons, sync buttons, sound, subtitles."""
from __future__ import annotations

import time
from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QFrame, QHBoxLayout, QLabel, QMenu, QSizePolicy, QSlider, QToolButton,
                               QVBoxLayout)

from ..sync.controller import fmt_time
from .qtutil import IconButton
from .seekbar import SeekBar


def _restyle(widget, name: str) -> None:
    if widget.objectName() != name:
        widget.setObjectName(name)
        widget.style().unpolish(widget)
        widget.style().polish(widget)


class ControlBar(QFrame):
    playClicked = Signal()
    backClicked = Signal()
    forwardClicked = Signal()
    syncUpClicked = Signal()
    syncMeClicked = Signal()
    followToggled = Signal(bool)
    muteClicked = Signal()
    volumeChanged = Signal(int)
    fullscreenClicked = Signal()
    sidebarClicked = Signal()
    settingsClicked = Signal()
    openClicked = Signal()

    def __init__(self, colors: dict, parent=None):
        super().__init__(parent)
        self.setObjectName("ControlBar")
        self.seek = SeekBar(colors)
        self.time_label = QLabel("0:00:00")
        self.right_label = QLabel("")
        self.right_label.setObjectName("Dim")
        row1 = QHBoxLayout()
        row1.addWidget(self.time_label)
        row1.addWidget(self.seek, 1)
        row1.addWidget(self.right_label)

        self.back = IconButton("back", "⏪ 10", "Skip back")
        self.play = IconButton("play", "▶  Play", "Play / pause (Space)")
        self.play.setMinimumWidth(92)
        self.forward = IconButton("forward", "10 ⏩", "Skip forward")
        self.sync_up = IconButton("sync_up", "⟲ Sync up", "Pause everyone, line up on the host, resume together")
        self.sync_me = IconButton("sync_me", "↪ Sync me", "Jump only yourself to where your friend is")
        self.follow = QToolButton()
        self.follow.setCheckable(True)
        self.follow.setChecked(True)
        self.follow.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.follow.toggled.connect(self._follow_toggled)
        self._follow_toggled(True, emit=False)
        self.gap_label = QLabel("")
        self.subs = IconButton("subtitles", "CC ▾", "Subtitles and audio track")
        self.subs.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.subs_menu = QMenu(self)
        self.subs.setMenu(self.subs_menu)
        self.mute = IconButton("volume", "🔊", "Mute (M)")
        self.volume = QSlider(Qt.Orientation.Horizontal)
        self.volume.setRange(0, 100)
        self.volume.setFixedWidth(110)
        self.volume.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.volume_label = QLabel("80%")
        self.volume_label.setMinimumWidth(38)
        self.fullscreen = IconButton("fullscreen", "⛶", "Fullscreen (F)")
        self.sidebar = IconButton("sidebar", "☰", "Show/hide the sidebar (Tab)")
        self.settings = IconButton("settings", "⚙", "Settings")
        self.open = IconButton("open", "📂", "Library: your films")

        self.back.clicked.connect(self.backClicked)
        self.play.clicked.connect(self.playClicked)
        self.forward.clicked.connect(self.forwardClicked)
        self.sync_up.clicked.connect(self.syncUpClicked)
        self.sync_me.clicked.connect(self.syncMeClicked)
        self.mute.clicked.connect(self.muteClicked)
        self.volume.valueChanged.connect(self.volumeChanged)
        self.fullscreen.clicked.connect(self.fullscreenClicked)
        self.sidebar.clicked.connect(self.sidebarClicked)
        self.settings.clicked.connect(self.settingsClicked)
        self.open.clicked.connect(self.openClicked)

        row2 = QHBoxLayout()
        for widget in (self.open, self.back, self.play, self.forward):
            row2.addWidget(widget)
        row2.addSpacing(10)
        for widget in (self.sync_up, self.sync_me, self.follow, self.gap_label):
            row2.addWidget(widget)
        row2.addStretch(1)
        for widget in (self.subs, self.mute, self.volume, self.volume_label, self.fullscreen,
                       self.sidebar, self.settings):
            row2.addWidget(widget)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 4, 10, 8)
        layout.setSpacing(2)
        layout.addLayout(row1)
        layout.addLayout(row2)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        # Room-only buttons start hidden (set_sync / set_following show them in a room). Visible at the
        # start, they made the window open about 90 px wider than asked, past the edge of small screens.
        for widget in (self.sync_up, self.sync_me, self.follow, self.gap_label):
            widget.hide()

    # ------------------------------------------------------------ updates from the main window
    def _follow_toggled(self, on: bool, emit: bool = True) -> None:
        self.follow.setText("🔗 Following the room" if on else "👤 On my own")
        self.follow.setToolTip("Your buttons control everyone. Click to watch on your own."
                               if on else "Your buttons only affect you. Click (or Sync me) to rejoin.")
        if emit:
            self.followToggled.emit(on)

    def set_following(self, on: bool, visible: bool) -> None:
        self.follow.setVisible(visible)
        if self.follow.isChecked() != on:
            self.follow.blockSignals(True)
            self.follow.setChecked(on)
            self.follow.blockSignals(False)
            self._follow_toggled(on, emit=False)

    def set_playing(self, playing: bool) -> None:
        if playing:
            self.play.set_face("pause", "⏸  Pause")
        else:
            self.play.set_face("play", "▶  Play")

    def set_times(self, position: float, duration: int, show_remaining: bool, show_end: bool) -> None:
        self.time_label.setText(fmt_time(position))
        if duration <= 0:
            self.right_label.setText("")
            return
        parts = [fmt_time(duration)]
        left = max(0.0, duration - position)
        if show_remaining:
            parts.append(f"−{fmt_time(left)}")
        if show_end:
            parts.append("ends " + time.strftime("%H:%M", time.localtime(time.time() + left / 1000.0)))
        self.right_label.setText("  ·  ".join(parts))

    def set_volume(self, percent: int, muted: bool) -> None:
        self.volume.blockSignals(True)
        self.volume.setValue(percent)
        self.volume.blockSignals(False)
        self.volume_label.setText("muted" if muted else f"{percent}%")
        if muted:
            self.mute.set_face("mute", "🔇")
        else:
            self.mute.set_face("volume", "🔊")

    def set_sync(self, connected: bool, can_sync_up: bool, cooldown_sync: float, gap_text: str,
                 attention: bool, gap_kind: str) -> None:
        self.sync_up.setVisible(connected)
        self.sync_me.setVisible(connected)
        self.sync_up.setEnabled(can_sync_up)
        label = "⟲ Sync up" + (f" ({cooldown_sync:.0f})" if cooldown_sync > 0 else "")
        if self.sync_up.text() and self.sync_up.text() != label:
            self.sync_up.setText(label)
        _restyle(self.sync_up, "Attention" if attention and can_sync_up else "")
        _restyle(self.sync_me, "Attention" if attention else "")
        self.gap_label.setVisible(connected and bool(gap_text))
        self.gap_label.setText(gap_text)
        _restyle(self.gap_label, gap_kind)

    def set_fullscreen(self, on: bool) -> None:
        if on:
            self.fullscreen.set_face("exit_fullscreen", "⛶ Exit")
        else:
            self.fullscreen.set_face("fullscreen", "⛶")
