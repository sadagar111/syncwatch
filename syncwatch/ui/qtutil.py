"""Small Qt helpers shared by the windows: images with fallbacks, round avatars, icon buttons."""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Dict, Optional

from PySide6.QtCore import QByteArray, QBuffer, QIODevice, QSize, Qt
from PySide6.QtGui import QBrush, QColor, QIcon, QImage, QImageReader, QPainter, QPainterPath, QPixmap
from PySide6.QtWidgets import QToolButton

from .. import errors, paths, theme as theme_module

log = logging.getLogger("syncwatch.ui")

_pixmaps: Dict[str, Optional[QPixmap]] = {}
_reported_missing = False


def asset_pixmap(name: str) -> Optional[QPixmap]:
    """A PNG from the assets folder, or None if it's missing/broken (reported once as SW-111)."""
    global _reported_missing
    if name in _pixmaps:
        return _pixmaps[name]
    path = paths.ASSETS_DIR / name
    pixmap = None
    if path.is_file():
        reader = QImageReader(str(path))
        image = reader.read()
        if not image.isNull():
            pixmap = QPixmap.fromImage(image)
    if pixmap is None and not _reported_missing:
        _reported_missing = True
        errors.report("SW-111", f"missing or unreadable: assets/{name}")
    _pixmaps[name] = pixmap
    return pixmap


def icon_or_none(name: str) -> Optional[QIcon]:
    pixmap = asset_pixmap(f"icons/{name}.png") if (paths.ASSETS_DIR / "icons" / f"{name}.png").exists() else None
    return QIcon(pixmap) if pixmap is not None else None


class IconButton(QToolButton):
    """A button that shows assets/icons/<name>.png if it exists, else its text label."""

    def __init__(self, icon_name: str, text: str, tooltip: str = "", parent=None):
        super().__init__(parent)
        self.icon_name = icon_name
        self.setToolTip(tooltip or text)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.set_face(icon_name, text)

    def set_face(self, icon_name: str, text: str) -> None:
        icon = icon_or_none(icon_name)
        if icon is not None:
            self.setIcon(icon)
            self.setIconSize(QSize(22, 22))
            self.setText("")
            self.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
        else:
            self.setIcon(QIcon())
            self.setText(text)
            self.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)


def round_pixmap(source: Optional[QPixmap], size: int, fallback_color: str = "#808080") -> QPixmap:
    """A circle-cropped copy of an image (or a plain circle if there's no image)."""
    ratio = 2  # draw at double size for crisp edges on high-DPI screens
    result = QPixmap(size * ratio, size * ratio)
    result.fill(Qt.GlobalColor.transparent)
    painter = QPainter(result)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
    clip = QPainterPath()
    clip.addEllipse(0, 0, size * ratio, size * ratio)
    painter.setClipPath(clip)
    if source is not None and not source.isNull():
        scaled = source.scaled(size * ratio, size * ratio, Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                               Qt.TransformationMode.SmoothTransformation)
        painter.drawPixmap((size * ratio - scaled.width()) // 2, (size * ratio - scaled.height()) // 2, scaled)
    else:
        painter.fillPath(clip, QBrush(QColor(fallback_color)))
    painter.end()
    result.setDevicePixelRatio(ratio)
    return result


def avatar_pixmap(png_bytes: Optional[bytes], size: int) -> QPixmap:
    """Round avatar from PNG/JPEG bytes, falling back to the default avatar."""
    source = None
    if png_bytes:
        image = image_from_bytes(png_bytes, max_side=512)
        if image is not None:
            source = QPixmap.fromImage(image)
    if source is None:
        theme, _ = theme_module.load(paths.ASSETS_DIR)
        source = asset_pixmap(theme.default_avatar)
    return round_pixmap(source, size)


def image_from_bytes(data: bytes, max_side: int = 512) -> Optional[QImage]:
    """Decode a small image safely: checks the size before decoding the pixels."""
    buffer = QBuffer()
    buffer.setData(QByteArray(data))
    buffer.open(QIODevice.OpenModeFlag.ReadOnly)
    reader = QImageReader(buffer)
    reader.setDecideFormatFromContent(True)
    size = reader.size()
    if not size.isValid() or size.width() > max_side or size.height() > max_side:
        return None
    image = reader.read()
    return None if image.isNull() else image


def png_bytes(image: QImage) -> bytes:
    data = QByteArray()
    buffer = QBuffer(data)
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    image.save(buffer, "PNG")
    buffer.close()
    return bytes(data.data())


def read_file_bytes(path: Path, limit: int) -> Optional[bytes]:
    try:
        if path.stat().st_size > limit:
            return None
        return path.read_bytes()
    except OSError:
        return None
