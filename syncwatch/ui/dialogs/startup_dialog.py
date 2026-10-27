"""The startup checklist window (shown always, only on problems, or never: your choice)."""
from __future__ import annotations

from typing import Callable, List

from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (QDialog, QGridLayout, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget)

from ... import errors
from ..checks import FAIL, OK, WARN, CheckResult

_MARK = {OK: ("✓", "Good"), WARN: ("!", "Warn"), FAIL: ("✕", "Bad")}


class StartupDialog(QDialog):
    def __init__(self, copy_report: Callable[[], str], parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_AlwaysShowToolTips)
        self.setWindowTitle("SyncWatch startup checks")
        self.setMinimumWidth(680)
        self.copy_report = copy_report
        self.results: List[CheckResult] = []
        self.grid = QGridLayout()
        self.grid.setColumnStretch(2, 1)
        holder = QWidget()
        holder.setLayout(self.grid)
        self.summary = QLabel("Checking…")
        copy = QPushButton("Copy report")
        copy.clicked.connect(self._copy)
        close = QPushButton("Continue")
        close.setObjectName("Accent")
        close.clicked.connect(self.accept)
        buttons = QHBoxLayout()
        buttons.addWidget(copy)
        buttons.addStretch(1)
        buttons.addWidget(close)
        layout = QVBoxLayout(self)
        layout.addWidget(self.summary)
        layout.addWidget(holder)
        layout.addLayout(buttons)

    def set_results(self, results: List[CheckResult]) -> None:
        order = {"App files": 0, "Video": 1, "Network": 2}
        self.results = sorted(results, key=lambda r: order.get(r.stage, 9))  # stable: keeps order within a stage
        while self.grid.count():
            item = self.grid.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        stage = None
        row = 0
        for result in self.results:
            if result.stage != stage:
                stage = result.stage
                title = QLabel(f"<b>{stage}</b>")
                self.grid.addWidget(title, row, 0, 1, 4)
                row += 1
            mark, style = _MARK[result.status]
            mark_label = QLabel(mark)
            mark_label.setObjectName(style)
            self.grid.addWidget(mark_label, row, 0)
            self.grid.addWidget(QLabel(result.name), row, 1)
            detail = QLabel(result.detail)
            detail.setToolTip(result.detail + (f"\n{result.technical}" if result.technical else ""))
            self.grid.addWidget(detail, row, 2)
            if result.code:
                more = QPushButton(f"What is {result.code}?")
                more.clicked.connect(lambda _=False, r=result: errors.report(r.code, r.technical, popup=True))
                self.grid.addWidget(more, row, 3)
            row += 1
        problems = sum(1 for r in self.results if r.status != OK)
        self.summary.setText("Everything looks good." if not problems else
                             f"{problems} thing(s) need attention. Watching may still work, see below.")
        self.adjustSize()

    def _copy(self) -> None:
        QGuiApplication.clipboard().setText(self.copy_report())
        self.summary.setText(self.summary.text().split(" (copied")[0] + " (copied to clipboard)")
