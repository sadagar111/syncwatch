"""Pictures over the seek bar (0.9): made once per film in the background, kept in the cache.

When a film from disk opens, its pictures are looked for in the profile's cache (by the film's
name and size); if they aren't there, library/previews.py makes them in a process of its own (at a
low priority, one film at a time; about 5-30 s for a whole film). The seek bar asks pixmap_at(ms)
while the mouse is over it. Streams (YouTube, torrents) get none. The cache keeps the newest 300
films' pictures (a few MB each at most).
"""
from __future__ import annotations

import collections
import hashlib
import json
import logging
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QObject, QPoint, QProcess, QSize, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPixmap
from PySide6.QtWidgets import QWidget

from ..library import previews

log = logging.getLogger("syncwatch.previews")
KEEP_FILES = 300


class Previews(QObject):
    ready = Signal()

    def __init__(self, settings, cache_dir: Path, parent=None):
        super().__init__(parent)
        self.settings = settings
        self.folder = cache_dir / "previews"
        self.file: Optional[previews.PreviewFile] = None
        self._pixmaps: "collections.OrderedDict[int, QPixmap]" = collections.OrderedDict()
        self._process: Optional[QProcess] = None
        self._target: Optional[Path] = None

    def film_opened(self, path: Path, key: str) -> None:
        self.clear()
        if not self.settings.get("playback.previews") or not key:
            return
        target = self.folder / (hashlib.sha1(key.encode("utf-8")).hexdigest()[:24] + ".swpv")
        if target.is_file():
            self._load(target)
            return
        try:
            self.folder.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            log.warning("no folder for seek-bar pictures: %s", exc)
            return
        self._target = target
        process = QProcess(self)
        process.setProcessChannelMode(QProcess.ProcessChannelMode.SeparateChannels)
        process.finished.connect(lambda _code, _status, p=process: self._finished(p))
        program, *arguments = previews.command(str(path), str(target), "--every",
                                               str(self.settings.get("playback.preview_every_s")))
        self._process = process
        process.start(program, arguments)

    def clear(self) -> None:
        """No pictures (a stream opens, the film closes); a picture-making process stops."""
        if self._process is not None:
            process, self._process = self._process, None
            process.finished.disconnect()
            process.kill()
            process.waitForFinished(2000)
            if self._target is not None:
                Path(str(self._target) + ".part").unlink(missing_ok=True)
        self.file, self._target = None, None
        self._pixmaps.clear()

    def stop(self) -> None:
        self.clear()

    def pixmap_at(self, ms: int) -> Optional[QPixmap]:
        if self.file is None:
            return None
        index = self.file.index_at(ms / 1000.0)
        if index in self._pixmaps:
            self._pixmaps.move_to_end(index)
            return self._pixmaps[index]
        data = self.file.picture(index)
        pixmap = QPixmap()
        if not data or not pixmap.loadFromData(data, "JPG"):
            return None
        self._pixmaps[index] = pixmap
        while len(self._pixmaps) > 80:
            self._pixmaps.popitem(last=False)
        return pixmap

    def _finished(self, process: QProcess) -> None:
        if process is not self._process:
            return
        self._process = None
        output = bytes(process.readAllStandardOutput()).decode("utf-8", "replace").splitlines()
        last = json.loads(output[-1]) if output and output[-1].startswith("{") else {}
        if "done" in last and self._target is not None and self._target.is_file():
            self._load(self._target)
            self._prune()
        else:
            log.info("no seek-bar pictures for this film: %s", last.get("detail") or last.get("error") or "no answer")

    def _load(self, path: Path) -> None:
        try:
            self.file = previews.PreviewFile(path)
        except (OSError, ValueError, KeyError) as exc:
            log.warning("seek-bar pictures unreadable (%s); made again next time", exc)
            path.unlink(missing_ok=True)
            return
        try:
            path.touch()                              # (kept longer: it's used)
        except OSError:
            pass
        self.ready.emit()

    def _prune(self) -> None:
        try:
            files = sorted(self.folder.glob("*.swpv"), key=lambda p: p.stat().st_mtime, reverse=True)
            for old in files[KEEP_FILES:]:
                old.unlink(missing_ok=True)
        except OSError:
            pass


class PreviewPopup(QWidget):
    """The picture and the time, floating just above the seek bar."""

    def __init__(self, parent=None):
        super().__init__(parent, Qt.WindowType.ToolTip | Qt.WindowType.FramelessWindowHint
                         | Qt.WindowType.WindowTransparentForInput | Qt.WindowType.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.pixmap: Optional[QPixmap] = None
        self.text = ""

    def show_at(self, pixmap: QPixmap, text: str, centre: QPoint) -> None:
        self.pixmap, self.text = pixmap, text
        size = QSize(pixmap.width() + 6, pixmap.height() + 26)
        self.resize(size)
        self.move(centre.x() - size.width() // 2, centre.y() - size.height() - 4)
        self.show()
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802 (Qt naming)
        if self.pixmap is None:
            return
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(12, 14, 18))
        painter.drawPixmap(3, 3, self.pixmap)
        painter.setPen(QColor("#E8EAED"))
        painter.drawText(self.rect().adjusted(0, self.pixmap.height() + 4, 0, 0),
                         int(Qt.AlignmentFlag.AlignCenter), self.text)
        painter.end()
