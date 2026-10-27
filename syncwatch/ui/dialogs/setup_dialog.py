"""First launch: your name, an optional picture and where your films are."""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QDialog, QFileDialog, QFormLayout, QHBoxLayout, QLabel, QLineEdit,
                               QPushButton, QVBoxLayout)

from ... import paths
from ..qtutil import avatar_pixmap
from .avatar_editor import choose_avatar


class SetupDialog(QDialog):
    def __init__(self, settings, avatar: Optional[bytes], first_time: bool = True, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_AlwaysShowToolTips)
        self.settings = settings
        self.avatar = avatar
        self.setWindowTitle("Welcome to SyncWatch" if first_time else "Your profile")
        self.setMinimumWidth(440)

        intro = QLabel("Watch films in sync with friends. First, tell your friends who you are."
                       if first_time else "This is what your friends see.")
        intro.setWordWrap(True)
        self.name = QLineEdit(settings.get("profile.name"))
        self.name.setMaxLength(32)
        self.name.setPlaceholderText("e.g. Alex")
        self.name.textChanged.connect(self._validate)

        self.picture = QLabel()
        choose = QPushButton("Choose picture…")
        choose.clicked.connect(self._choose)
        self.remove = QPushButton("Remove")
        self.remove.clicked.connect(self._remove)
        picture_row = QHBoxLayout()
        picture_row.addWidget(self.picture)
        picture_row.addWidget(choose)
        picture_row.addWidget(self.remove)
        picture_row.addStretch(1)

        self.folder = QLineEdit(settings.get("paths.movies_dir"))
        self.folder.setPlaceholderText(str(paths.DEFAULT_MOVIES_DIR))
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse)
        folder_row = QHBoxLayout()
        folder_row.addWidget(self.folder, 1)
        folder_row.addWidget(browse)

        form = QFormLayout()
        form.addRow("Your name", self.name)
        form.addRow("Picture (optional)", picture_row)
        form.addRow("Films folder", folder_row)
        hint = QLabel("Everything here can be changed later in Settings.")
        hint.setObjectName("Hint")

        self.ok = QPushButton("Start watching" if first_time else "Save")
        self.ok.setObjectName("Accent")
        self.ok.setDefault(True)
        self.ok.clicked.connect(self._save)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        if not first_time:
            cancel = QPushButton("Cancel")
            cancel.clicked.connect(self.reject)
            buttons.addWidget(cancel)
        buttons.addWidget(self.ok)

        layout = QVBoxLayout(self)
        layout.addWidget(intro)
        layout.addLayout(form)
        layout.addWidget(hint)
        layout.addLayout(buttons)
        self._refresh_picture()
        self._validate()

    def _validate(self) -> None:
        self.ok.setEnabled(bool(self.name.text().strip()))

    def _refresh_picture(self) -> None:
        self.picture.setPixmap(avatar_pixmap(self.avatar, 48))
        self.remove.setEnabled(self.avatar is not None)

    def _choose(self) -> None:
        data = choose_avatar(self)
        if data is not None:
            self.avatar = data
            self._refresh_picture()

    def _remove(self) -> None:
        self.avatar = None
        self._refresh_picture()

    def _browse(self) -> None:
        start = self.folder.text() or str(paths.DEFAULT_MOVIES_DIR)
        folder = QFileDialog.getExistingDirectory(self, "Films folder", start)
        if folder:
            self.folder.setText(folder)

    def _save(self) -> None:
        self.settings.set("profile.name", self.name.text().strip())
        folder = self.folder.text().strip()
        if folder and Path(folder) == paths.DEFAULT_MOVIES_DIR:
            folder = ""
        self.settings.set("paths.movies_dir", folder)
        self.settings.save()
        self.accept()

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if event.key() == Qt.Key.Key_Escape and not self.ok.isEnabled():
            return  # first launch: a name is needed
        super().keyPressEvent(event)
