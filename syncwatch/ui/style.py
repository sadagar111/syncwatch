"""SyncWatch's widget style: Qt's "Fusion" look plus readable checkboxes and radio buttons.

Why: with a dark theme, Qt draws the tick in the light text colour on a light
box, so ticks were nearly invisible (reported in 0.2.0). This style draws the
box, tick and radio dot itself, in colours from assets/theme.json
("check_box", "check_border", "check_mark").
"""
from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QProxyStyle, QStyle, QStyleFactory

INDICATOR_PX = 16


class SyncWatchStyle(QProxyStyle):
    def __init__(self, theme):
        super().__init__(QStyleFactory.create("Fusion"))
        self.box = QColor(theme.colors.get("check_box", "#F2F4F7")[:7])
        self.border = QColor(theme.colors.get("check_border", "#9AA3B2")[:7])
        self.mark = QColor(theme.colors.get("check_mark", "#000000")[:7])
        self.accent = QColor(theme.colors.get("accent", "#E5484D")[:7])

    def pixelMetric(self, metric, option=None, widget=None):  # noqa: N802 (Qt naming)
        if metric in (QStyle.PixelMetric.PM_IndicatorWidth, QStyle.PixelMetric.PM_IndicatorHeight,
                      QStyle.PixelMetric.PM_ExclusiveIndicatorWidth, QStyle.PixelMetric.PM_ExclusiveIndicatorHeight):
            return INDICATOR_PX
        return super().pixelMetric(metric, option, widget)

    def drawPrimitive(self, element, option, painter, widget=None):  # noqa: N802
        if element in (QStyle.PrimitiveElement.PE_IndicatorCheckBox,
                       QStyle.PrimitiveElement.PE_IndicatorItemViewItemCheck,
                       QStyle.PrimitiveElement.PE_IndicatorMenuCheckMark):
            self._check_box(option, painter)
            return
        if element == QStyle.PrimitiveElement.PE_IndicatorRadioButton:
            self._radio(option, painter)
            return
        super().drawPrimitive(element, option, painter, widget)

    # ------------------------------------------------------------ drawing
    def _colours(self, option):
        enabled = bool(option.state & QStyle.StateFlag.State_Enabled)
        hovered = bool(option.state & QStyle.StateFlag.State_MouseOver)
        box = QColor(self.box) if enabled else QColor(self.box).darker(160)
        border = self.accent if (hovered and enabled) else self.border
        mark = QColor(self.mark) if enabled else QColor("#6B7280")
        return box, border, mark

    def _square(self, option) -> QRectF:
        rect = QRectF(option.rect)
        side = min(rect.width(), rect.height(), INDICATOR_PX)
        return QRectF(rect.center().x() - side / 2 + 0.5, rect.center().y() - side / 2 + 0.5, side - 1, side - 1)

    def _check_box(self, option, painter: QPainter) -> None:
        box, border, mark = self._colours(option)
        r = self._square(option)
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(border, 1))
        painter.setBrush(box)
        painter.drawRoundedRect(r, 3, 3)
        if option.state & QStyle.StateFlag.State_On:
            path = QPainterPath()
            path.moveTo(QPointF(r.left() + r.width() * 0.22, r.top() + r.height() * 0.53))
            path.lineTo(QPointF(r.left() + r.width() * 0.42, r.top() + r.height() * 0.73))
            path.lineTo(QPointF(r.left() + r.width() * 0.79, r.top() + r.height() * 0.29))
            pen = QPen(mark, max(1.8, r.width() * 0.14))
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawPath(path)
        elif option.state & QStyle.StateFlag.State_NoChange:
            painter.setPen(QPen(mark, 2, cap=Qt.PenCapStyle.RoundCap))
            y = r.center().y()
            painter.drawLine(QPointF(r.left() + 4, y), QPointF(r.right() - 4, y))
        painter.restore()

    def _radio(self, option, painter: QPainter) -> None:
        box, border, mark = self._colours(option)
        r = self._square(option)
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(border, 1))
        painter.setBrush(box)
        painter.drawEllipse(r)
        if option.state & QStyle.StateFlag.State_On:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(mark)
            inset = r.width() * 0.3
            painter.drawEllipse(r.adjusted(inset, inset, -inset, -inset))
        painter.restore()
