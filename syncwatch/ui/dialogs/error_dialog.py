"""The error popup: plain explanation first, then the code and technical details.

It doesn't block the app (you can keep watching), at most a few are open at
once, and "Copy details" puts everything needed for a bug report on the
clipboard.
"""
from __future__ import annotations

from typing import List

from PySide6.QtCore import QUrl, Qt
from PySide6.QtGui import QDesktopServices, QGuiApplication
from PySide6.QtWidgets import (QDialog, QHBoxLayout, QLabel, QPlainTextEdit, QPushButton, QVBoxLayout)

from ...errors import ERROR, WARNING, Report

_open: List["ErrorDialog"] = []
MAX_OPEN = 3
_SEVERITY = {ERROR: ("●", "#F0506E", "Problem"), WARNING: ("●", "#F5A524", "Warning"),
             "info": ("●", "#4DA3FF", "Note")}


class ErrorDialog(QDialog):
    def __init__(self, report: Report, logs_dir=None, parent=None):
        super().__init__(parent)
        self.report = report
        dot, colour, word = _SEVERITY.get(report.problem.severity, _SEVERITY[ERROR])
        self.setWindowTitle(f"SyncWatch: {word} ({report.code})")
        self.setMinimumWidth(460)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)

        title = QLabel(f"<span style='color:{colour}'>{dot}</span>&nbsp; <b>{_esc(report.title)}</b>")
        title.setTextFormat(Qt.TextFormat.RichText)
        title.setStyleSheet("font-size: 13pt;")
        explanation = QLabel(_esc(report.explanation))
        explanation.setWordWrap(True)
        layout = QVBoxLayout(self)
        layout.addWidget(title)
        layout.addWidget(explanation)
        if report.tips:
            tips = QLabel("<b>What you can try:</b><ul style='margin-left:-20px'>" +
                          "".join(f"<li>{_esc(t)}</li>" for t in report.tips) + "</ul>")
            tips.setTextFormat(Qt.TextFormat.RichText)
            tips.setWordWrap(True)
            layout.addWidget(tips)

        code = QLabel(f"Error code: <b>{report.code}</b>")
        code.setObjectName("Dim")
        code.setTextFormat(Qt.TextFormat.RichText)
        layout.addSpacing(6)
        layout.addWidget(code)
        self.details = QPlainTextEdit(report.details)
        self.details.setReadOnly(True)
        self.details.setMaximumHeight(110)
        self.details.setStyleSheet("font-family: Consolas, 'DejaVu Sans Mono', monospace; font-size: 8.5pt;")
        self.details.setVisible(False)
        toggle = QPushButton("Show technical details")
        toggle.setCheckable(True)
        toggle.toggled.connect(lambda on: (self.details.setVisible(on),
                                           toggle.setText("Hide technical details" if on else "Show technical details"),
                                           self.adjustSize()))
        layout.addWidget(toggle, alignment=Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(self.details)

        buttons = QHBoxLayout()
        copy = QPushButton("Copy details")
        copy.clicked.connect(self._copy)
        self.copied = QLabel("")
        self.copied.setObjectName("Dim")
        buttons.addWidget(copy)
        buttons.addWidget(self.copied)
        buttons.addStretch(1)
        if logs_dir is not None:
            open_logs = QPushButton("Open log folder")
            open_logs.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(logs_dir))))
            buttons.addWidget(open_logs)
        close = QPushButton("OK")
        close.setObjectName("Accent")
        close.setDefault(True)
        close.clicked.connect(self.close)
        buttons.addWidget(close)
        layout.addLayout(buttons)

    def _copy(self) -> None:
        QGuiApplication.clipboard().setText(self.report.details)
        self.copied.setText("Copied. Paste it into a message.")

    def closeEvent(self, event) -> None:  # noqa: N802
        if self in _open:
            _open.remove(self)
        super().closeEvent(event)


def show(report: Report, logs_dir=None, parent=None) -> None:
    """Show a popup without blocking; never more than MAX_OPEN at once."""
    while len(_open) >= MAX_OPEN:
        _open.pop(0).close()
    dialog = ErrorDialog(report, logs_dir, parent)
    _open.append(dialog)
    dialog.show()
    dialog.raise_()


def _esc(text: str) -> str:
    return (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
