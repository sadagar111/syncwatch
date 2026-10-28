"""The update window: what's new, then download progress, then "restart now?"."""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QDialog, QHBoxLayout, QLabel, QProgressBar, QPushButton, QStackedWidget,
                               QTextBrowser, QVBoxLayout, QWidget)

from ... import __version__


def human_size(size: float) -> str:
    return f"{size / 1e6:.1f} MB" if size < 1e9 else f"{size / 1e9:.2f} GB"


def human_time(seconds) -> str:
    if seconds is None:
        return "estimating…"
    seconds = int(seconds + 0.5)
    if seconds < 60:
        return f"about {seconds} s left"
    return f"about {seconds // 60} min {seconds % 60:02d} s left"


class UpdateDialog(QDialog):
    updateClicked = Signal()
    skipClicked = Signal()
    cancelClicked = Signal()
    restartClicked = Signal()

    def __init__(self, result, connected_to: str = "", parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_AlwaysShowToolTips)
        found = result.manifest
        self.setWindowTitle(f"SyncWatch {found.version}")
        self.setMinimumSize(520, 420)
        self.pages = QStackedWidget()

        # ---- page 1: what's new
        info = QWidget()
        info_layout = QVBoxLayout(info)
        kind = "test version " if found.version.is_test else "version "
        title = QLabel(f"<b>SyncWatch {kind}{found.version} is available</b> (you have {__version__})")
        title.setStyleSheet("font-size: 12pt;")
        notes = QTextBrowser()
        notes.setOpenExternalLinks(False)
        notes.setMarkdown(found.notes or "No release notes.")
        size = f"Download: {human_size(result.download_bytes)}"
        if result.needs_runtime:
            size += " (includes a new video engine, so it's bigger than usual)"
        size_label = QLabel(size)
        size_label.setObjectName("Dim")
        update = QPushButton("Update now")
        update.setObjectName("Accent")
        update.setDefault(True)
        update.clicked.connect(self.updateClicked)
        skip = QPushButton("Skip this version")
        skip.setToolTip("Don't remind me about this version. You can still update in Settings > Updates.")
        skip.clicked.connect(lambda: (self.skipClicked.emit(), self.reject()))
        later = QPushButton("Later")
        later.clicked.connect(self.reject)
        buttons = QHBoxLayout()
        buttons.addWidget(skip)
        buttons.addStretch(1)
        buttons.addWidget(later)
        buttons.addWidget(update)
        info_layout.addWidget(title)
        info_layout.addWidget(QLabel("What's new:"))
        info_layout.addWidget(notes, 1)
        info_layout.addWidget(size_label)
        info_layout.addLayout(buttons)

        # ---- page 2: downloading
        progress_page = QWidget()
        progress_layout = QVBoxLayout(progress_page)
        self.progress_title = QLabel(f"Downloading SyncWatch {found.version}…")
        self.bar = QProgressBar()
        self.bar.setRange(0, 1000)
        self.progress_text = QLabel("Starting…")
        self.progress_text.setObjectName("Dim")
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.cancelClicked)
        cancel_row = QHBoxLayout()
        cancel_row.addStretch(1)
        cancel_row.addWidget(cancel)
        hint = QLabel("You can keep watching while it downloads.")
        hint.setObjectName("Hint")
        progress_layout.addStretch(1)
        progress_layout.addWidget(self.progress_title)
        progress_layout.addWidget(self.bar)
        progress_layout.addWidget(self.progress_text)
        progress_layout.addWidget(hint)
        progress_layout.addStretch(1)
        progress_layout.addLayout(cancel_row)

        # ---- page 3: ready
        ready = QWidget()
        ready_layout = QVBoxLayout(ready)
        ready_text = f"<b>SyncWatch {found.version} is ready.</b><br>Restart SyncWatch to finish updating."
        if connected_to:
            ready_text += (f"<br><br>You're in {connected_to}. Restarting takes you out for a moment; "
                           "you get straight back in afterwards.")
        ready_label = QLabel(ready_text)
        ready_label.setWordWrap(True)
        restart = QPushButton("Restart now")
        restart.setObjectName("Accent")
        restart.clicked.connect(self.restartClicked)
        not_now = QPushButton("Later")
        not_now.setToolTip("The new version starts the next time you open SyncWatch.")
        not_now.clicked.connect(self.accept)
        ready_buttons = QHBoxLayout()
        ready_buttons.addStretch(1)
        ready_buttons.addWidget(not_now)
        ready_buttons.addWidget(restart)
        ready_layout.addStretch(1)
        ready_layout.addWidget(ready_label)
        ready_layout.addStretch(1)
        ready_layout.addLayout(ready_buttons)

        for page in (info, progress_page, ready):
            self.pages.addWidget(page)
        layout = QVBoxLayout(self)
        layout.addWidget(self.pages)

    def show_progress(self) -> None:
        self.pages.setCurrentIndex(1)

    def show_ready(self) -> None:
        self.pages.setCurrentIndex(2)

    def show_info(self) -> None:
        self.pages.setCurrentIndex(0)

    def set_progress(self, done: int, total: int, speed: float, eta) -> None:
        self.bar.setValue(int(1000 * done / total) if total else 0)
        speed_text = f" · {human_size(speed)}/s" if speed > 0 else ""
        self.progress_text.setText(f"{human_size(done)} of {human_size(total)}{speed_text} · "
                                   f"{human_time(eta) if done < total else 'checking…'}")
