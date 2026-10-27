"""Subtitle timing: any delay you like, plus "stretch" for frame-rate mismatches.

Delay: positive = subtitles appear later, negative = earlier. Any value works
(e.g. -37.5 s for subtitles made for a version with a longer intro).
Stretch: if subtitles start in sync but drift further off over time, they
were made for a version of the film with another frame rate; pick the
matching fix, or type an exact factor.
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QComboBox, QDialog, QDoubleSpinBox, QFormLayout, QHBoxLayout, QLabel,
                               QPushButton, QVBoxLayout)

from ...subtitles.model import STRETCH_PRESETS


class SubtitleTimingDialog(QDialog):
    def __init__(self, subtitles, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_AlwaysShowToolTips)
        self.subs = subtitles
        self.setWindowTitle("Subtitle timing")
        self.setMinimumWidth(460)

        self.delay = QDoubleSpinBox()
        self.delay.setRange(-36000.0, 36000.0)
        self.delay.setDecimals(3)
        self.delay.setSingleStep(0.1)
        self.delay.setSuffix(" s")
        self.delay.setValue(subtitles.delay_ms / 1000.0)
        self.delay.valueChanged.connect(lambda v: self.subs.set_delay(int(round(v * 1000))))
        nudges = QHBoxLayout()
        for label, delta in (("−1 s", -1.0), ("−0.1 s", -0.1), ("+0.1 s", 0.1), ("+1 s", 1.0)):
            button = QPushButton(label)
            button.clicked.connect(lambda _=False, d=delta: self.delay.setValue(self.delay.value() + d))
            nudges.addWidget(button)

        self.preset = QComboBox()
        for text, factor in STRETCH_PRESETS:
            self.preset.addItem(text, factor)
        self.preset.addItem("Custom factor", None)
        self.factor = QDoubleSpinBox()
        self.factor.setRange(0.5, 2.0)
        self.factor.setDecimals(5)
        self.factor.setSingleStep(0.001)
        self.factor.setValue(subtitles.stretch)
        self._select_preset(subtitles.stretch)
        self.preset.currentIndexChanged.connect(self._preset_changed)
        self.factor.valueChanged.connect(self._factor_changed)

        explain = QLabel("Positive delay = subtitles later. Use stretch if subtitles start in sync but "
                         "drift further off as the film goes on.")
        explain.setObjectName("Hint")
        explain.setWordWrap(True)
        form = QFormLayout()
        form.addRow("Delay", self.delay)
        form.addRow("", nudges)
        form.addRow("Stretch", self.preset)
        form.addRow("Factor", self.factor)
        reset = QPushButton("Reset")
        reset.clicked.connect(self._reset)
        close = QPushButton("Close")
        close.setObjectName("Accent")
        close.clicked.connect(self.accept)
        buttons = QHBoxLayout()
        buttons.addWidget(reset)
        buttons.addStretch(1)
        buttons.addWidget(close)
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(explain)
        layout.addLayout(buttons)

    def _select_preset(self, factor: float) -> None:
        for index in range(self.preset.count() - 1):
            if abs(self.preset.itemData(index) - factor) < 1e-6:
                self.preset.setCurrentIndex(index)
                return
        self.preset.setCurrentIndex(self.preset.count() - 1)

    def _preset_changed(self, index: int) -> None:
        factor = self.preset.itemData(index)
        if factor is not None:
            self.factor.blockSignals(True)
            self.factor.setValue(factor)
            self.factor.blockSignals(False)
            self.subs.set_stretch(factor)

    def _factor_changed(self, value: float) -> None:
        self.subs.set_stretch(value)
        self.preset.blockSignals(True)
        self._select_preset(value)
        self.preset.blockSignals(False)

    def _reset(self) -> None:
        self.delay.setValue(0.0)
        self.factor.setValue(1.0)
