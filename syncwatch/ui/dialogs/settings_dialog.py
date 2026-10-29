"""The settings window, built automatically from ``settings.SCHEMA``.

Every change applies immediately. Each row has a ↺ button to put that one
setting back to its default. Advanced settings only appear when "Show
advanced options" is ticked; when they're hidden but some were changed, a
note says so, so nothing changes behaviour "invisibly".
"""
from __future__ import annotations

from typing import Callable, Dict, List, Optional

from PySide6.QtCore import QPoint, QTimer, QUrl, Qt
from PySide6.QtGui import QColor, QDesktopServices, QFontDatabase, QKeySequence
from PySide6.QtWidgets import (QCheckBox, QColorDialog, QComboBox, QDialog, QDoubleSpinBox, QFileDialog,
                               QGridLayout, QHBoxLayout, QKeySequenceEdit, QLabel, QLineEdit, QMessageBox,
                               QPushButton, QScrollArea, QSpinBox, QTabWidget, QToolButton, QToolTip,
                               QVBoxLayout, QWidget)

from ... import settings as settings_module
from ...settings import SCHEMA, SECTIONS, Setting


class InfoBadge(QLabel):
    """The ⓘ next to a setting. Shows its explanation when you hover over it (after a
    short moment) or click it.

    It shows the text itself instead of relying on Qt's automatic tooltips,
    which didn't appear on Windows in 0.2.0.
    """
    HOVER_DELAY_MS = 350

    def __init__(self, text: str, parent=None):
        super().__init__("ⓘ", parent)
        self.help = text
        self.setObjectName("Dim")
        self.setCursor(Qt.CursorShape.WhatsThisCursor)
        self.setToolTip(text)  # keep the normal tooltip too, for screen readers etc.
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(self.HOVER_DELAY_MS)
        self._timer.timeout.connect(self.show_help)

    def show_help(self) -> None:
        QToolTip.showText(self.mapToGlobal(QPoint(self.width(), self.height())), self.help, self)

    def enterEvent(self, event) -> None:  # noqa: N802 (Qt naming)
        self._timer.start()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802
        self._timer.stop()
        super().leaveEvent(event)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        self._timer.stop()
        self.show_help()


class SettingsDialog(QDialog):
    def __init__(self, settings, extras: Optional[Dict[str, Callable[[QVBoxLayout], None]]] = None, parent=None):
        """``extras`` maps a section name to a function that adds custom widgets to its tab."""
        super().__init__(parent)
        self.settings = settings
        self.setWindowTitle("SyncWatch settings")
        self.setAttribute(Qt.WidgetAttribute.WA_AlwaysShowToolTips)
        self.inline_help: List[QLabel] = []
        self.resize(780, 580)
        self.rows: Dict[str, List[QWidget]] = {}
        self.editors: Dict[str, QWidget] = {}
        self.resets: Dict[str, QToolButton] = {}
        self._loading = False

        self.advanced = QCheckBox("Show advanced options")
        self.advanced.setChecked(settings.get("ui.show_advanced"))
        self.advanced.toggled.connect(self._advanced_toggled)
        self.tabs = QTabWidget()
        self.section_tabs: Dict[str, int] = {}
        for section in SECTIONS:
            page = QWidget()
            column = QVBoxLayout(page)
            grid = QGridLayout()
            grid.setColumnStretch(1, 1)
            grid.setHorizontalSpacing(10)
            grid.setVerticalSpacing(8)
            row = 0
            for setting in SCHEMA:
                if setting.section != section or setting.hidden or setting.key == "ui.show_advanced":
                    continue
                row = self._add_row(grid, row, setting)
            column.addLayout(grid)
            if extras and section in extras:
                extras[section](column)
            if section == "Keyboard":
                self.key_warning = QLabel("")
                self.key_warning.setObjectName("Warn")
                self.key_warning.setWordWrap(True)
                column.addWidget(self.key_warning)
            column.addStretch(1)
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setWidget(page)
            self.section_tabs[section] = self.tabs.addTab(scroll, section)

        self.note = QLabel("")
        self.note.setObjectName("Hint")
        self.note.setWordWrap(True)
        reset_all = QPushButton("Reset all to defaults")
        reset_all.clicked.connect(self._reset_all)
        close = QPushButton("Close")
        close.setObjectName("Accent")
        close.clicked.connect(self.accept)
        buttons = QHBoxLayout()
        buttons.addWidget(reset_all)
        buttons.addStretch(1)
        buttons.addWidget(close)
        layout = QVBoxLayout(self)
        layout.addWidget(self.advanced)
        layout.addWidget(self.tabs, 1)
        layout.addWidget(self.note)
        layout.addLayout(buttons)
        self._apply_visibility()
        self._check_keys()

    # ------------------------------------------------------------ building rows
    def _add_row(self, grid: QGridLayout, row: int, setting: Setting) -> int:
        """Adds one setting (two grid rows if it has an explanation). Returns the next free row."""
        label_box = QWidget()
        label_row = QHBoxLayout(label_box)
        label_row.setContentsMargins(0, 0, 0, 0)
        label_row.setSpacing(5)
        label = QLabel(setting.label)
        label.setWordWrap(True)
        label.setToolTip(setting.help)
        label_row.addWidget(label, 1)
        if setting.help:
            label_row.addWidget(InfoBadge(setting.help), 0, Qt.AlignmentFlag.AlignTop)
        editor = self._editor(setting)
        if setting.help:
            editor.setToolTip(setting.help)
        reset = QToolButton()
        reset.setText("↺")
        reset.setToolTip("Back to default")
        reset.clicked.connect(lambda _=False, k=setting.key: self._reset(k))
        grid.addWidget(label_box, row, 0)
        grid.addWidget(editor, row, 1)
        grid.addWidget(reset, row, 2)
        widgets = [label_box, editor, reset]
        if setting.help:
            explanation = QLabel(setting.help)
            explanation.setObjectName("Hint")
            explanation.setWordWrap(True)
            grid.addWidget(explanation, row + 1, 0, 1, 3)
            self.inline_help.append(explanation)
            widgets.append(explanation)
        self.rows[setting.key] = widgets
        self.editors[setting.key] = editor
        self.resets[setting.key] = reset
        self._load_value(setting)
        return row + (2 if setting.help else 1)

    def _editor(self, setting: Setting) -> QWidget:
        key, kind = setting.key, setting.kind
        if kind == "bool":
            box = QCheckBox()
            box.toggled.connect(lambda on: self._set(key, on))
            return box
        if kind == "int":
            spin = QSpinBox()
            spin.setRange(int(setting.minimum or 0), int(setting.maximum if setting.maximum is not None else 1_000_000))
            spin.setSuffix(setting.suffix)
            spin.valueChanged.connect(lambda v: self._set(key, v))
            return spin
        if kind == "float":
            spin = QDoubleSpinBox()
            spin.setDecimals(setting.decimals)
            spin.setRange(float(setting.minimum or 0), float(setting.maximum if setting.maximum is not None else 1e6))
            spin.setSingleStep(setting.step or 0.1)
            spin.setSuffix(setting.suffix)
            spin.valueChanged.connect(lambda v: self._set(key, v))
            return spin
        if kind == "choice":
            combo = QComboBox()
            for value, text in setting.choices or []:
                combo.addItem(text, value)
            combo.currentIndexChanged.connect(lambda i, c=combo: self._set(key, c.itemData(i)))
            return combo
        if kind == "color":
            button = QPushButton()
            button.clicked.connect(lambda: self._pick_color(key))
            return button
        if kind == "font":
            combo = QComboBox()
            combo.addItem("Default", "")
            for family in QFontDatabase.families():
                combo.addItem(family, family)
            combo.currentIndexChanged.connect(lambda i, c=combo: self._set(key, c.itemData(i)))
            return combo
        if kind == "keys":
            wrapper = QWidget()
            row = QHBoxLayout(wrapper)
            row.setContentsMargins(0, 0, 0, 0)
            edit = QKeySequenceEdit()
            if hasattr(edit, "setMaximumSequenceLength"):
                edit.setMaximumSequenceLength(1)
            edit.editingFinished.connect(lambda e=edit: self._set(key, e.keySequence().toString(QKeySequence.SequenceFormat.PortableText)))
            clear = QToolButton()
            clear.setText("✕")
            clear.setToolTip("No shortcut")
            clear.clicked.connect(lambda _=False, e=edit: (e.clear(), self._set(key, "")))
            row.addWidget(edit, 1)
            row.addWidget(clear)
            wrapper.edit = edit  # type: ignore[attr-defined]
            return wrapper
        line = QLineEdit()
        line.setMaxLength(setting.max_len)
        line.editingFinished.connect(lambda l=line: self._set(key, l.text()))
        if kind == "path":
            wrapper = QWidget()
            row = QHBoxLayout(wrapper)
            row.setContentsMargins(0, 0, 0, 0)
            browse = QPushButton("Browse…")
            browse.clicked.connect(lambda _=False, l=line: self._browse(key, l))
            row.addWidget(line, 1)
            row.addWidget(browse)
            wrapper.line = line  # type: ignore[attr-defined]
            return wrapper
        return line

    def _load_value(self, setting: Setting) -> None:
        self._loading = True
        try:
            editor = self.editors[setting.key]
            value = self.settings.get(setting.key)
            kind = setting.kind
            if kind == "bool":
                editor.setChecked(bool(value))
            elif kind in ("int", "float"):
                editor.setValue(value)
            elif kind in ("choice", "font"):
                index = editor.findData(value)
                editor.setCurrentIndex(max(0, index))
            elif kind == "color":
                editor.setText(value)
                text_colour = "#000000" if QColor(value).lightness() > 128 else "#FFFFFF"
                editor.setStyleSheet(f"background: {value}; color: {text_colour};")
            elif kind == "keys":
                editor.edit.setKeySequence(QKeySequence(value))
            elif kind == "path":
                editor.line.setText(value)
            else:
                editor.setText(value)
            self.resets[setting.key].setEnabled(not self.settings.is_default(setting.key))
        finally:
            self._loading = False

    # ------------------------------------------------------------ changes
    def _set(self, key: str, value) -> None:
        if self._loading:
            return
        self.settings.set(key, value)
        self._load_value(settings_module.BY_KEY[key])  # shows the cleaned value (e.g. clamped)
        self._update_note()
        if key == "ui.settings_help_inline":
            self._apply_visibility()
        if key.startswith("keys."):
            self._check_keys()

    def _reset(self, key: str) -> None:
        self.settings.reset(key)
        self._load_value(settings_module.BY_KEY[key])
        self._update_note()
        self._check_keys()

    def _reset_all(self) -> None:
        answer = QMessageBox.question(self, "Reset all settings",
                                      "Put every setting back to its default? Your name and picture stay.")
        if answer != QMessageBox.StandardButton.Yes:
            return
        name = self.settings.get("profile.name")
        self.settings.reset()
        self.settings.set("profile.name", name)
        for setting in SCHEMA:
            if setting.key in self.editors:
                self._load_value(setting)
        self.advanced.setChecked(self.settings.get("ui.show_advanced"))
        self._update_note()
        self._check_keys()

    def _pick_color(self, key: str) -> None:
        current = QColor(self.settings.get(key))
        colour = QColorDialog.getColor(current, self, "Choose a colour")
        if colour.isValid():
            self._set(key, colour.name().upper())

    def _browse(self, key: str, line: QLineEdit) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Choose a folder", line.text() or "")
        if folder:
            line.setText(folder)
            self._set(key, folder)

    def _advanced_toggled(self, on: bool) -> None:
        self.settings.set("ui.show_advanced", on)
        self._apply_visibility()

    def _apply_visibility(self) -> None:
        show = self.advanced.isChecked()
        inline = bool(self.settings.get("ui.settings_help_inline"))
        for setting in SCHEMA:
            for widget in self.rows.get(setting.key, []):
                visible = show or not setting.advanced
                if widget in self.inline_help:
                    visible = visible and inline
                widget.setVisible(visible)
        for section, index in self.section_tabs.items():
            has_basic = any(s.section == section and not s.advanced and not s.hidden for s in SCHEMA)
            self.tabs.setTabVisible(index, show or has_basic or section in ("Profile", "Diagnostics"))
        self._update_note()

    def _update_note(self) -> None:
        changed = self.settings.changed_advanced()
        if not self.advanced.isChecked() and changed:
            self.note.setText(f"{len(changed)} advanced setting(s) differ from the defaults and still apply. "
                              "Tick \"Show advanced options\" to see them.")
        else:
            self.note.setText("Changes apply immediately.")

    def _check_keys(self) -> None:
        used: Dict[str, str] = {}
        clashes = []
        for setting in SCHEMA:
            if setting.kind != "keys":
                continue
            value = self.settings.get(setting.key)
            if not value:
                continue
            if value in used:
                clashes.append(f"{value}: {used[value]} and {setting.label}")
            used[value] = setting.label
        if hasattr(self, "key_warning"):
            self.key_warning.setText("Same key used twice, so neither works: " + "; ".join(clashes) if clashes else "")


def diagnostics_extras(logs_dir, copy_report: Callable[[], None], rerun_checks: Callable[[], None]):
    """Buttons for the Diagnostics tab."""
    def add(column: QVBoxLayout) -> None:
        row = QHBoxLayout()
        open_logs = QPushButton("Open log folder")
        open_logs.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(logs_dir))))
        report = QPushButton("Copy report")
        report.setToolTip("Copies versions, startup check results and the recent log. Paste it into a message "
                          "when asking for help.")
        report.clicked.connect(copy_report)
        checks = QPushButton("Run startup checks again")
        checks.clicked.connect(rerun_checks)
        for button in (open_logs, report, checks):
            row.addWidget(button)
        row.addStretch(1)
        column.addLayout(row)
    return add


def profile_extras(get_avatar: Callable[[], Optional[bytes]], change_avatar: Callable[[], None],
                   remove_avatar: Callable[[], None]):
    """Picture controls for the Profile tab."""
    from ..qtutil import avatar_pixmap

    def add(column: QVBoxLayout) -> None:
        row = QHBoxLayout()
        picture = QLabel()
        picture.setPixmap(avatar_pixmap(get_avatar(), 48))
        change = QPushButton("Change picture…")
        remove = QPushButton("Remove picture")

        def refresh() -> None:
            picture.setPixmap(avatar_pixmap(get_avatar(), 48))
            remove.setEnabled(get_avatar() is not None)

        change.clicked.connect(lambda: (change_avatar(), refresh()))
        remove.clicked.connect(lambda: (remove_avatar(), refresh()))
        refresh()
        row.addWidget(QLabel("Picture"))
        row.addWidget(picture)
        row.addWidget(change)
        row.addWidget(remove)
        row.addStretch(1)
        column.addLayout(row)
    return add


def updates_extras(controller, window):
    """Version info and buttons for the Updates tab."""
    def add(column: QVBoxLayout) -> None:
        info = QLabel(controller.describe())
        info.setWordWrap(True)
        row = QHBoxLayout()
        check = QPushButton("Check now")
        check.clicked.connect(lambda: (controller.check(manual=True), info.setText(controller.describe())))
        row.addWidget(check)
        whats_new = QPushButton("What's new…")
        whats_new.clicked.connect(controller.open_dialog)
        row.addWidget(whats_new)
        back_version = controller.can_go_back()
        back = QPushButton(f"Go back to version {back_version}" if back_version else "Go back")
        back.setToolTip("Restarts SyncWatch with the version you had before the last update.")
        back.setVisible(bool(back_version))
        back.clicked.connect(controller.go_back)
        row.addWidget(back)
        row.addStretch(1)

        def refresh() -> None:
            info.setText(controller.describe())
            whats_new.setVisible(controller.result is not None and controller.result.status == "update")

        refresh()
        window.settings_dialog_refresh = refresh
        column.addWidget(info)
        column.addLayout(row)
    return add


def rooms_extras(controller):
    """Your ID, and the people you blocked (with Unblock), for the Watching together tab."""
    def add(column: QVBoxLayout) -> None:
        store = controller.rooms.store
        identity = controller.rooms.identity.public
        relay = controller.rooms.default_relay()
        info = QLabel(f"Your ID: <b>{identity.short_id}</b> &nbsp;·&nbsp; relay server: "
                      f"{relay or 'not set up yet (rooms are off)'}")
        info.setWordWrap(True)
        info.setToolTip("Your ID is made from this PC's own keys. Friends see it next to your name, so "
                        "nobody can pretend to be you by using your name.")
        column.addWidget(info)
        blocked_box = QVBoxLayout()
        column.addLayout(blocked_box)

        def refresh() -> None:
            while blocked_box.count():
                item = blocked_box.takeAt(0)
                if item.widget() is not None:
                    item.widget().deleteLater()
                elif item.layout() is not None:
                    while item.layout().count():
                        inner = item.layout().takeAt(0)
                        if inner.widget() is not None:
                            inner.widget().deleteLater()
            title = QLabel("Blocked people:" if store.blocked else "Blocked people: nobody")
            blocked_box.addWidget(title)
            for key_id, blocked in sorted(store.blocked.items(), key=lambda item: item[1].name.lower()):
                row = QHBoxLayout()
                name = QLabel(f"{blocked.name} ({blocked.key.short_id})")
                name.setTextFormat(Qt.TextFormat.PlainText)   # whatever name they gave: never as HTML
                row.addWidget(name)
                unblock = QPushButton("Unblock")
                unblock.clicked.connect(lambda _=False, k=key_id: (store.unblock(k), refresh()))
                row.addWidget(unblock)
                row.addStretch(1)
                blocked_box.addLayout(row)

        refresh()
    return add
