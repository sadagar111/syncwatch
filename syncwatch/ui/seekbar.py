"""The seek bar: click or drag to jump, hover to see the time, friends shown as markers."""
from __future__ import annotations

from typing import List, Optional, Tuple

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QBrush, QColor, QFont, QFontMetricsF, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QSizePolicy, QToolTip, QWidget

from ..sync.controller import fmt_time


class SeekBar(QWidget):
    seekRequested = Signal(int)      # ms, when the user lets go
    previewChanged = Signal(int)     # ms, while dragging (for the time label)

    TRACK_Y = 22
    TRACK_H = 6

    def __init__(self, colors: dict, parent=None):
        super().__init__(parent)
        self.colors = colors
        self.setMouseTracking(True)
        self.setMinimumHeight(32)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.duration = 0
        self.position = 0
        self.markers: List[Tuple[int, str, str]] = []   # (ms, colour, name)
        self.show_hover_time = True
        self.show_markers = True
        self._hover_x: Optional[float] = None
        self._drag_ms: Optional[int] = None

    # ------------------------------------------------------------ data
    def set_state(self, position: float, duration: int) -> None:
        position = int(position)
        if position != self.position or duration != self.duration:
            self.position, self.duration = position, duration
            self.update()

    def set_markers(self, markers: List[Tuple[int, str, str]]) -> None:
        if markers != self.markers:
            self.markers = markers
            self.update()

    @property
    def dragging(self) -> bool:
        return self._drag_ms is not None

    # ------------------------------------------------------------ geometry
    def _track(self) -> QRectF:
        return QRectF(8, self.TRACK_Y, max(1, self.width() - 16), self.TRACK_H)

    def _ms_at(self, x: float) -> int:
        track = self._track()
        if self.duration <= 0:
            return 0
        fraction = min(1.0, max(0.0, (x - track.left()) / track.width()))
        return int(fraction * self.duration)

    def _x_of(self, ms: float) -> float:
        track = self._track()
        if self.duration <= 0:
            return track.left()
        return track.left() + track.width() * min(1.0, max(0.0, ms / self.duration))

    # ------------------------------------------------------------ mouse
    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton and self.duration > 0:
            self._drag_ms = self._ms_at(event.position().x())
            self.previewChanged.emit(self._drag_ms)
            self.update()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        self._hover_x = event.position().x()
        if self._drag_ms is not None:
            self._drag_ms = self._ms_at(self._hover_x)
            self.previewChanged.emit(self._drag_ms)
        marker = self._marker_near(event.position())
        if marker:
            QToolTip.showText(event.globalPosition().toPoint(), f"{marker[2]}: {fmt_time(marker[0])}", self)
        self.update()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton and self._drag_ms is not None:
            target = self._drag_ms
            self._drag_ms = None
            self.position = target
            self.seekRequested.emit(target)
            self.update()

    def leaveEvent(self, event) -> None:  # noqa: N802
        self._hover_x = None
        self.update()

    def _marker_near(self, point: QPointF):
        if not self.show_markers:
            return None
        for marker in self.markers:
            if abs(self._x_of(marker[0]) - point.x()) < 7 and point.y() < self.TRACK_Y:
                return marker
        return None

    # ------------------------------------------------------------ painting
    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        track = self._track()
        shown = self._drag_ms if self._drag_ms is not None else self.position
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(self.colors.get("seek_track", "#3A4150")))
        painter.drawRoundedRect(track, 3, 3)
        filled = QRectF(track.left(), track.top(), self._x_of(shown) - track.left(), track.height())
        painter.setBrush(QColor(self.colors.get("seek_fill", "#E5484D")))
        painter.drawRoundedRect(filled, 3, 3)
        if self.show_markers:
            for ms, colour, _name in self.markers:
                x = self._x_of(ms)
                tri = QPainterPath()
                tri.moveTo(x, track.top() - 2)
                tri.lineTo(x - 5, track.top() - 10)
                tri.lineTo(x + 5, track.top() - 10)
                tri.closeSubpath()
                painter.fillPath(tri, QBrush(QColor(colour)))
        handle_x = self._x_of(shown)
        painter.setBrush(QColor(self.colors.get("text", "#E8EAED")))
        painter.setPen(QPen(QColor(0, 0, 0, 90), 1))
        painter.drawEllipse(QPointF(handle_x, track.center().y()), 7, 7)
        hover_x = handle_x if self._drag_ms is not None else self._hover_x
        if self.show_hover_time and hover_x is not None and self.duration > 0:
            text = fmt_time(self._ms_at(hover_x))
            font = QFont()
            font.setPixelSize(11)
            painter.setFont(font)
            metrics = QFontMetricsF(font)
            width = metrics.horizontalAdvance(text) + 10
            left = min(max(0.0, hover_x - width / 2), self.width() - width)
            box = QRectF(left, 0, width, 15)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(0, 0, 0, 190))
            painter.drawRoundedRect(box, 4, 4)
            painter.setPen(QColor("#FFFFFF"))
            painter.drawText(box, Qt.AlignmentFlag.AlignCenter, text)
        painter.end()
