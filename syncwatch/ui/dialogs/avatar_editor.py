"""A tiny picture editor for your profile picture: drag, zoom, rotate, done.

The picture is cropped to a square, shrunk to 128 x 128 and re-saved by the
app, so nobody has to prepare their image beforehand. Huge photos are loaded
at reduced size so the app never freezes or runs out of memory, and phone
photos are turned the right way up automatically.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from PySide6.QtCore import QPointF, QRect, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QImage, QImageReader, QPainter, QPainterPath, QPixmap, QTransform
from PySide6.QtWidgets import (QDialog, QHBoxLayout, QLabel, QPushButton, QSlider, QVBoxLayout, QWidget)

from ... import errors
from ..qtutil import png_bytes, round_pixmap

OUTPUT_SIZE = 128
MAX_FILE_BYTES = 100 * 1024 * 1024
MAX_LOAD_SIDE = 2048
MAX_OUTPUT_BYTES = 90 * 1024


def load_picture(path: Path) -> Optional[QImage]:
    """Read a picture safely, or report SW-801 / SW-802 and return None."""
    try:
        size = path.stat().st_size
    except OSError as exc:
        errors.report("SW-801", errors.describe_exception(exc))
        return None
    if size > MAX_FILE_BYTES:
        errors.report("SW-802", f"{size / 1e6:.0f} MB", limit_mb=MAX_FILE_BYTES // (1024 * 1024))
        return None
    QImageReader.setAllocationLimit(512)  # MB; refuses absurd images instead of using all memory
    reader = QImageReader(str(path))
    reader.setAutoTransform(True)
    dimensions = reader.size()
    if dimensions.isValid() and max(dimensions.width(), dimensions.height()) > MAX_LOAD_SIDE:
        dimensions.scale(MAX_LOAD_SIDE, MAX_LOAD_SIDE, Qt.AspectRatioMode.KeepAspectRatio)
        reader.setScaledSize(dimensions)
    image = reader.read()
    if image.isNull():
        errors.report("SW-801", f"{reader.errorString()} ({path.suffix})")
        return None
    return image.convertToFormat(QImage.Format.Format_ARGB32)


def encode_avatar(image: QImage) -> bytes:
    """Final small file: PNG normally, JPEG if a busy photo would make the PNG too big."""
    data = png_bytes(image)
    if len(data) <= MAX_OUTPUT_BYTES:
        return data
    from PySide6.QtCore import QBuffer, QByteArray, QIODevice
    for quality in (88, 75, 60):
        buffer_data = QByteArray()
        buffer = QBuffer(buffer_data)
        buffer.open(QIODevice.OpenModeFlag.WriteOnly)
        image.convertToFormat(QImage.Format.Format_RGB32).save(buffer, "JPEG", quality)
        buffer.close()
        if len(buffer_data) <= MAX_OUTPUT_BYTES:
            return bytes(buffer_data.data())
    return bytes(buffer_data.data())


class CropCanvas(QWidget):
    changed = Signal()
    SIZE = 300
    CIRCLE = 250

    def __init__(self, image: QImage, parent=None):
        super().__init__(parent)
        self.setFixedSize(self.SIZE, self.SIZE)
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        self.image = image
        self.zoom = 1.0           # 1.0 = the picture just covers the circle
        self.offset = QPointF()   # top-left of the picture on the canvas
        self._drag_from: Optional[QPointF] = None
        self._center()

    def _base_scale(self) -> float:
        return self.CIRCLE / max(1, min(self.image.width(), self.image.height()))

    def scale(self) -> float:
        return self._base_scale() * self.zoom

    def _center(self) -> None:
        s = self.scale()
        self.offset = QPointF((self.SIZE - self.image.width() * s) / 2, (self.SIZE - self.image.height() * s) / 2)
        self._clamp()

    def _clamp(self) -> None:
        """Keep the circle fully covered by the picture."""
        s = self.scale()
        left = (self.SIZE - self.CIRCLE) / 2
        right = left + self.CIRCLE
        x = min(left, max(right - self.image.width() * s, self.offset.x()))
        y = min(left, max(right - self.image.height() * s, self.offset.y()))
        self.offset = QPointF(x, y)

    def set_zoom(self, zoom: float) -> None:
        center = QPointF(self.SIZE / 2, self.SIZE / 2)
        old = self.scale()
        image_point = (center - self.offset) / old
        self.zoom = max(1.0, min(8.0, zoom))
        self.offset = center - image_point * self.scale()
        self._clamp()
        self.update()
        self.changed.emit()

    def rotate(self) -> None:
        self.image = self.image.transformed(QTransform().rotate(90), Qt.TransformationMode.SmoothTransformation)
        self.zoom = 1.0
        self._center()
        self.update()
        self.changed.emit()

    def result(self) -> QImage:
        s = self.scale()
        left = (self.SIZE - self.CIRCLE) / 2
        source = QRectF((left - self.offset.x()) / s, (left - self.offset.y()) / s, self.CIRCLE / s, self.CIRCLE / s)
        crop = self.image.copy(source.toAlignedRect())
        return crop.scaled(OUTPUT_SIZE, OUTPUT_SIZE, Qt.AspectRatioMode.IgnoreAspectRatio,
                           Qt.TransformationMode.SmoothTransformation)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        self._drag_from = event.position()
        self.setCursor(Qt.CursorShape.ClosedHandCursor)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if self._drag_from is not None:
            self.offset += event.position() - self._drag_from
            self._drag_from = event.position()
            self._clamp()
            self.update()
            self.changed.emit()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        self._drag_from = None
        self.setCursor(Qt.CursorShape.OpenHandCursor)

    def wheelEvent(self, event) -> None:  # noqa: N802
        steps = event.angleDelta().y() / 120.0
        self.set_zoom(self.zoom * (1.12 ** steps))
        parent = self.parent()
        if hasattr(parent, "sync_slider"):
            parent.sync_slider()

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), QColor("#0B0D12"))
        s = self.scale()
        target = QRectF(self.offset.x(), self.offset.y(), self.image.width() * s, self.image.height() * s)
        painter.drawImage(target, self.image)
        shade = QPainterPath()
        shade.addRect(QRectF(self.rect()))
        left = (self.SIZE - self.CIRCLE) / 2
        circle = QPainterPath()
        circle.addEllipse(QRectF(left, left, self.CIRCLE, self.CIRCLE))
        painter.fillPath(shade.subtracted(circle), QColor(0, 0, 0, 150))
        painter.setPen(QColor(255, 255, 255, 200))
        painter.drawEllipse(QRectF(left, left, self.CIRCLE, self.CIRCLE))
        painter.end()


class AvatarEditor(QDialog):
    def __init__(self, image: QImage, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Your profile picture")
        self.canvas = CropCanvas(image, self)
        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(100, 800)
        self.slider.valueChanged.connect(lambda v: self.canvas.set_zoom(v / 100.0))
        rotate = QPushButton("Rotate ⟳")
        rotate.clicked.connect(self.canvas.rotate)
        self.preview_big = QLabel()
        self.preview_small = QLabel()
        self.canvas.changed.connect(self._update_preview)

        hint = QLabel("Drag to move · mouse wheel or slider to zoom")
        hint.setObjectName("Hint")
        side = QVBoxLayout()
        side.addWidget(QLabel("Preview"))
        side.addWidget(self.preview_big)
        side.addWidget(self.preview_small)
        side.addStretch(1)
        top = QHBoxLayout()
        top.addWidget(self.canvas)
        top.addLayout(side)
        zoom_row = QHBoxLayout()
        zoom_row.addWidget(QLabel("Zoom"))
        zoom_row.addWidget(self.slider, 1)
        zoom_row.addWidget(rotate)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        save = QPushButton("Use this picture")
        save.setObjectName("Accent")
        save.setDefault(True)
        save.clicked.connect(self.accept)
        buttons.addWidget(cancel)
        buttons.addWidget(save)
        layout = QVBoxLayout(self)
        layout.addLayout(top)
        layout.addWidget(hint)
        layout.addLayout(zoom_row)
        layout.addLayout(buttons)
        self._update_preview()

    def sync_slider(self) -> None:
        self.slider.blockSignals(True)
        self.slider.setValue(int(self.canvas.zoom * 100))
        self.slider.blockSignals(False)

    def _update_preview(self) -> None:
        pixmap = QPixmap.fromImage(self.canvas.result())
        self.preview_big.setPixmap(round_pixmap(pixmap, 64))
        self.preview_small.setPixmap(round_pixmap(pixmap, 28))

    def avatar_bytes(self) -> bytes:
        return encode_avatar(self.canvas.result())


def choose_avatar(parent=None) -> Optional[bytes]:
    """Ask for a picture file, open the editor, return the finished picture bytes (or None)."""
    from PySide6.QtWidgets import QFileDialog
    path, _ = QFileDialog.getOpenFileName(parent, "Choose a picture", str(Path.home()),
                                          "Pictures (*.png *.jpg *.jpeg *.webp *.bmp *.gif);;All files (*)")
    if not path:
        return None
    image = load_picture(Path(path))
    if image is None:
        return None
    editor = AvatarEditor(image, parent)
    if editor.exec() == QDialog.DialogCode.Accepted:
        return editor.avatar_bytes()
    return None
