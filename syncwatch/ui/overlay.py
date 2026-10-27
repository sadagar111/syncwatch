"""The see-through layer over the video: subtitles, messages, the 3-2-1 countdown.

Why a separate window: Qt shows video in its own native window (checked with
Qt 6.12), and on Windows nothing from the main window can be drawn on top of
a native window. A frameless, transparent, click-through window that follows
the video area is the reliable way. It never takes focus or clicks, so the
video and keyboard shortcuts work exactly as without it.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

from PySide6.QtCore import QPointF, QRect, QRectF, Qt, QTimer
from PySide6.QtGui import (QBrush, QColor, QFont, QFontMetricsF, QPainter, QPainterPath, QPen)
from PySide6.QtWidgets import QWidget

_TAG = re.compile(r"(</?[ibu]>)")


@dataclass
class SubtitleStyle:
    size_pct: float = 5.0
    color: str = "#FFFFFF"
    outline_px: int = 3
    outline_color: str = "#000000"
    box: bool = False
    box_opacity: int = 55
    font_family: str = ""
    bold: bool = False
    margin_pct: float = 6.0


@dataclass
class _Run:
    text: str
    bold: bool
    italic: bool
    underline: bool


def parse_markup(markup: str) -> List[List[_Run]]:
    """'<i>Hi</i> there\\nline 2' -> lines of styled runs."""
    lines: List[List[_Run]] = [[]]
    state = {"i": False, "b": False, "u": False}
    for part in _TAG.split(markup):
        if not part:
            continue
        tag = _TAG.fullmatch(part)
        if tag:
            name = part.strip("</>")
            state[name] = not part.startswith("</")
            continue
        chunks = part.split("\n")
        for index, chunk in enumerate(chunks):
            if index > 0:
                lines.append([])
            if chunk:
                lines[-1].append(_Run(chunk, state["b"], state["i"], state["u"]))
    return [line for line in lines if line]


class Overlay(QWidget):
    TOAST_COLORS = {"info": "#FFFFFF", "you": "#9BE8B4", "friend": "#9CC9FF", "warn": "#FFC46B",
                    "error": "#FF8A9B"}

    def __init__(self, owner: QWidget):
        super().__init__(owner, Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint
                         | Qt.WindowType.WindowTransparentForInput | Qt.WindowType.WindowDoesNotAcceptFocus
                         | Qt.WindowType.NoDropShadowWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.style = SubtitleStyle()
        self.subtitles_enabled = True
        self._cues: Tuple[Tuple[str, bool], ...] = ()
        self._toasts: List[Tuple[str, str, float]] = []
        self._countdown_end: Optional[float] = None
        self._flash: Optional[Tuple[str, float]] = None
        self._help: Optional[str] = None
        self._timer = QTimer(self)
        self._timer.setInterval(100)
        self._timer.timeout.connect(self._animate)
        self._timer.start()
        self.toast_seconds = 5.0

    # ------------------------------------------------------------ content
    def set_subtitles(self, cues: Sequence[Tuple[str, bool]]) -> None:
        cues = tuple(cues)
        if cues != self._cues:
            self._cues = cues
            self.update()

    def set_style(self, style: SubtitleStyle) -> None:
        self.style = style
        self.update()

    def toast(self, text: str, kind: str = "info") -> None:
        self._toasts.append((text, kind, time.monotonic() + self.toast_seconds))
        self._toasts = self._toasts[-4:]
        self.update()

    def countdown(self, ms: int) -> None:
        self._countdown_end = time.monotonic() + ms / 1000.0 if ms > 0 else None
        self.update()

    def flash(self, symbol: str) -> None:
        self._flash = (symbol, time.monotonic() + 0.6)
        self.update()

    def show_help(self, text: Optional[str]) -> None:
        self._help = text
        self.update()

    @property
    def help_visible(self) -> bool:
        return self._help is not None

    def follow(self, area: QRect) -> None:
        """Place the overlay exactly over the video area (global screen coordinates)."""
        if area != self.geometry():
            self.setGeometry(area)

    def _animate(self) -> None:
        now = time.monotonic()
        changed = False
        alive = [t for t in self._toasts if t[2] > now]
        if len(alive) != len(self._toasts):
            self._toasts = alive
            changed = True
        if self._countdown_end is not None:
            if now >= self._countdown_end:
                self._countdown_end = None
            changed = True
        if self._flash is not None:
            changed = True
            if now >= self._flash[1]:
                self._flash = None
        if changed:
            self.update()

    # ------------------------------------------------------------ painting
    def paintEvent(self, event) -> None:  # noqa: N802 (Qt naming)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        rect = self.rect()
        if self.subtitles_enabled and self._cues:
            self._paint_subtitles(painter, rect)
        self._paint_toasts(painter, rect)
        if self._countdown_end is not None:
            seconds = max(1, int(self._countdown_end - time.monotonic() + 0.999))
            self._paint_center(painter, rect, str(seconds), 0.16)
        elif self._flash is not None:
            symbol, until = self._flash
            opacity = max(0.0, min(1.0, (until - time.monotonic()) / 0.6))
            painter.setOpacity(opacity)
            self._paint_center(painter, rect, symbol, 0.09)
            painter.setOpacity(1.0)
        if self._help:
            self._paint_help(painter, rect)
        painter.end()

    def _font(self, size_px: float, bold: bool = False, italic: bool = False, underline: bool = False) -> QFont:
        font = QFont(self.style.font_family) if self.style.font_family else QFont()
        font.setPixelSize(max(8, int(size_px)))
        font.setBold(bold or self.style.bold)
        font.setItalic(italic)
        font.setUnderline(underline)
        return font

    def _wrap(self, line: List[_Run], size_px: float, max_width: float) -> List[List[_Run]]:
        words: List[_Run] = []
        for run in line:
            for word in re.findall(r"\S+\s*|\s+", run.text):
                words.append(_Run(word, run.bold, run.italic, run.underline))
        result: List[List[_Run]] = [[]]
        width = 0.0
        for word in words:
            metrics = QFontMetricsF(self._font(size_px, word.bold, word.italic))
            w = metrics.horizontalAdvance(word.text)
            if result[-1] and width + metrics.horizontalAdvance(word.text.rstrip()) > max_width:
                result.append([])
                width = 0.0
                word = _Run(word.text.lstrip(), word.bold, word.italic, word.underline)
                w = metrics.horizontalAdvance(word.text)
            result[-1].append(word)
            width += w
        return [r for r in result if r]

    def _paint_subtitles(self, painter: QPainter, rect: QRect) -> None:
        style = self.style
        size_px = max(10.0, rect.height() * style.size_pct / 100.0)
        line_height = size_px * 1.25
        margin = rect.height() * style.margin_pct / 100.0
        max_width = rect.width() * 0.9
        bottom_lines: List[List[_Run]] = []
        top_lines: List[List[_Run]] = []
        for markup, top in self._cues:
            target = top_lines if top else bottom_lines
            for line in parse_markup(markup):
                target.extend(self._wrap(line, size_px, max_width))
        y = rect.height() - margin - line_height * len(bottom_lines)
        for line in bottom_lines:
            self._paint_line(painter, rect, line, size_px, y + size_px)
            y += line_height
        y = margin
        for line in top_lines:
            self._paint_line(painter, rect, line, size_px, y + size_px)
            y += line_height

    def _paint_line(self, painter: QPainter, rect: QRect, line: List[_Run], size_px: float, baseline: float) -> None:
        style = self.style
        widths = []
        for run in line:
            widths.append(QFontMetricsF(self._font(size_px, run.bold, run.italic)).horizontalAdvance(run.text))
        total = sum(widths)
        x = (rect.width() - total) / 2.0
        path = QPainterPath()
        for run, width in zip(line, widths):
            path.addText(QPointF(x, baseline), self._font(size_px, run.bold, run.italic, run.underline), run.text)
            x += width
        if style.box:
            bounds = path.boundingRect().adjusted(-size_px * 0.35, -size_px * 0.15, size_px * 0.35, size_px * 0.2)
            box = QColor(0, 0, 0, int(255 * style.box_opacity / 100))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QBrush(box))
            painter.drawRoundedRect(bounds, size_px * 0.2, size_px * 0.2)
        if style.outline_px > 0:
            pen = QPen(QColor(style.outline_color), style.outline_px * 2.0)
            pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            painter.strokePath(path, pen)
        painter.fillPath(path, QBrush(QColor(style.color)))

    def _paint_toasts(self, painter: QPainter, rect: QRect) -> None:
        if not self._toasts:
            return
        font = QFont()
        font.setPixelSize(max(13, int(rect.height() * 0.022)))
        painter.setFont(font)
        metrics = QFontMetricsF(font)
        y = 16.0
        for text, kind, _ in self._toasts:
            width = min(rect.width() * 0.6, metrics.horizontalAdvance(text) + 24)
            box = QRectF(16, y, width, metrics.height() + 14)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(0, 0, 0, 175))
            painter.drawRoundedRect(box, 8, 8)
            painter.setPen(QColor(self.TOAST_COLORS.get(kind, "#FFFFFF")))
            elided = metrics.elidedText(text, Qt.TextElideMode.ElideRight, width - 24)
            painter.drawText(box.adjusted(12, 0, -12, 0), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, elided)
            y += box.height() + 8

    def _paint_center(self, painter: QPainter, rect: QRect, text: str, scale: float) -> None:
        size = max(28.0, rect.height() * scale)
        circle = QRectF(rect.width() / 2 - size, rect.height() / 2 - size, size * 2, size * 2)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(0, 0, 0, 150))
        painter.drawEllipse(circle)
        font = QFont()
        font.setPixelSize(int(size))
        font.setBold(True)
        painter.setFont(font)
        painter.setPen(QColor("#FFFFFF"))
        painter.drawText(circle, Qt.AlignmentFlag.AlignCenter, text)

    def _paint_help(self, painter: QPainter, rect: QRect) -> None:
        """Lines with a tab are drawn as two columns (key, what it does)."""
        font = QFont()
        font.setPixelSize(max(13, int(rect.height() * 0.024)))
        bold = QFont(font)
        bold.setBold(True)
        metrics, bold_metrics = QFontMetricsF(font), QFontMetricsF(bold)
        lines = [line.split("\t", 1) for line in self._help.split("\n")]
        key_width = max((bold_metrics.horizontalAdvance(p[0]) for p in lines if len(p) == 2), default=0) + 24
        width = max((key_width + metrics.horizontalAdvance(p[1])) if len(p) == 2 else metrics.horizontalAdvance(p[0])
                    for p in lines) + 40
        height = metrics.height() * len(lines) + 30
        box = QRectF((rect.width() - width) / 2, (rect.height() - height) / 2, width, height)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(0, 0, 0, 215))
        painter.drawRoundedRect(box, 10, 10)
        painter.setPen(QColor("#FFFFFF"))
        y = box.top() + 15 + metrics.ascent()
        for parts in lines:
            if len(parts) == 2:
                painter.setFont(bold)
                painter.drawText(QPointF(box.left() + 20, y), parts[0])
                painter.setFont(font)
                painter.drawText(QPointF(box.left() + 20 + key_width, y), parts[1])
            else:
                painter.setFont(font)
                painter.drawText(QPointF(box.left() + 20, y), parts[0])
            y += metrics.height()
