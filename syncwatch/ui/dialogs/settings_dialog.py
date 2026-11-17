"""The settings window, built automatically from ``settings.SCHEMA``.

One page, top to bottom (since 0.5.2), like most apps and websites: a list of the sections on
the left to jump, a search box at the top, and in every section its usual settings, the
section's own buttons, then a folded "Advanced" part with the rest. "Advanced (6) · 2 changed"
says when something in there differs from the defaults, so nothing changes behaviour unseen.

Every change applies immediately. Each row has a ↺ button to put that one setting back to its
default.
"""
from __future__ import annotations

from typing import Callable, Dict, List, Optional

from PySide6.QtCore import QPoint, QTimer, QUrl, Qt
from PySide6.QtGui import QColor, QDesktopServices, QFontDatabase, QKeySequence, QShortcut
from PySide6.QtWidgets import (QCheckBox, QColorDialog, QComboBox, QDialog, QDoubleSpinBox, QFileDialog,
                               QGridLayout, QHBoxLayout, QKeySequenceEdit, QLabel, QLineEdit, QListWidget,
                               QListWidgetItem, QMessageBox, QPushButton, QScrollArea, QSpinBox, QToolButton,
                               QToolTip, QVBoxLayout, QWidget)

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


class _Section:
    """One part of the settings page: its heading, the usual settings, the section's own buttons
    ("extras"), then a folded Advanced part with the rest."""

    def __init__(self, name: str):
        self.name = name
        self.frame = QWidget()
        self.layout = QVBoxLayout(self.frame)
        self.layout.setContentsMargins(0, 6, 0, 18)
        self.layout.setSpacing(8)
        self.heading = QLabel(name)
        self.heading.setObjectName("SettingsHeading")
        self.layout.addWidget(self.heading)
        self.grid = _grid()
        self.layout.addLayout(self.grid)
        self.extras: Optional[QWidget] = None
        self.toggle: Optional[QToolButton] = None
        self.advanced_box: Optional[QWidget] = None
        self.advanced_grid: Optional[QGridLayout] = None
        self.basic_keys: List[str] = []
        self.advanced_keys: List[str] = []
        self.open = False                  # the Advanced part is unfolded (when not searching)


def _grid() -> QGridLayout:
    grid = QGridLayout()
    grid.setColumnStretch(1, 1)
    grid.setHorizontalSpacing(10)
    grid.setVerticalSpacing(8)
    return grid


LABEL_WIDTH = 280       # every section's labels as wide: the controls line up all the way down
EDITOR_WIDTH = 340      # text boxes and lists no wider than this (a folder or a key a bit more)
ADVANCED_INDENT = 14    # the Advanced part starts this much further right, behind a thin line


class SettingsDialog(QDialog):
    """One page, top to bottom: every section with its settings, the advanced ones folded away at
    the end of each section. The list on the left jumps to a section; the search box shows only
    the settings that match (advanced ones included)."""

    def __init__(self, settings, extras: Optional[Dict[str, Callable[[QVBoxLayout], None]]] = None, parent=None):
        """``extras`` maps a section name to a function that adds custom widgets to its section."""
        super().__init__(parent)
        self.settings = settings
        self.setWindowTitle("SyncWatch settings")
        self.setAttribute(Qt.WidgetAttribute.WA_AlwaysShowToolTips)
        self.inline_help: List[QLabel] = []
        self.resize(940, 660)
        self.rows: Dict[str, List[QWidget]] = {}
        self.editors: Dict[str, QWidget] = {}
        self.resets: Dict[str, QToolButton] = {}
        self._loading = False
        self._searching = False

        self.search = QLineEdit()
        self.search.setPlaceholderText("🔍  Search settings")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(lambda text: self._filter(scroll_up=True))

        self.nav = QListWidget()
        self.nav.setObjectName("SettingsNav")
        self.nav.setFixedWidth(200)
        self.nav.currentRowChanged.connect(self._nav_clicked)

        page = QWidget()
        self.page_layout = QVBoxLayout(page)
        self.page_layout.setContentsMargins(4, 0, 16, 0)
        self.sections: Dict[str, _Section] = {}
        for name in SECTIONS:
            section = _Section(name)
            for setting in SCHEMA:
                if setting.section != name or setting.hidden:
                    continue
                if setting.advanced:
                    section.advanced_keys.append(setting.key)
                else:
                    section.basic_keys.append(setting.key)
            row = 0
            for key in section.basic_keys:
                row = self._add_row(section.grid, row, settings_module.BY_KEY[key])
            if extras and name in extras:
                section.extras = QWidget()
                extra_layout = QVBoxLayout(section.extras)
                extra_layout.setContentsMargins(0, 0, 0, 0)
                extras[name](extra_layout)
                section.layout.addWidget(section.extras)
            if any(settings_module.BY_KEY[k].kind == "keys" for k in section.basic_keys + section.advanced_keys):
                self.key_warning = QLabel("")
                self.key_warning.setObjectName("Warn")
                self.key_warning.setWordWrap(True)
                section.layout.addWidget(self.key_warning)
            if section.advanced_keys:
                section.toggle = QToolButton()
                section.toggle.setObjectName("AdvancedToggle")
                section.toggle.setCursor(Qt.CursorShape.PointingHandCursor)
                section.toggle.clicked.connect(
                    lambda _=False, n=name: self.set_advanced_open(n, not self.sections[n].open))
                section.layout.addWidget(section.toggle, alignment=Qt.AlignmentFlag.AlignLeft)
                section.advanced_box = QWidget()
                section.advanced_box.setObjectName("AdvancedBox")
                section.advanced_grid = _grid()
                box_layout = QVBoxLayout(section.advanced_box)
                box_layout.setContentsMargins(ADVANCED_INDENT - 2, 2, 0, 2)      # (2: the line on the left)
                box_layout.addLayout(section.advanced_grid)
                row = 0
                for key in section.advanced_keys:
                    row = self._add_row(section.advanced_grid, row, settings_module.BY_KEY[key], ADVANCED_INDENT)
                section.layout.addWidget(section.advanced_box)
            self.sections[name] = section
            self.page_layout.addWidget(section.frame)
            QListWidgetItem(name, self.nav)
        self.nothing = QLabel("")
        self.nothing.setObjectName("Dim")
        self.nothing.setWordWrap(True)
        self.page_layout.addWidget(self.nothing)
        self.page_layout.addStretch(1)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll.setWidget(page)
        self.scroll.verticalScrollBar().valueChanged.connect(self._scrolled)

        self.note = QLabel("Changes apply immediately.")
        self.note.setObjectName("Hint")
        self.note.setWordWrap(True)
        reset_all = QPushButton("Reset all to defaults")
        reset_all.clicked.connect(self._reset_all)
        close = QPushButton("Close")
        close.setObjectName("Accent")
        close.clicked.connect(self.accept)
        buttons = QHBoxLayout()
        buttons.addWidget(reset_all)
        buttons.addWidget(self.note, 1)
        buttons.addWidget(close)
        middle = QHBoxLayout()
        middle.setSpacing(12)
        middle.addWidget(self.nav)
        middle.addWidget(self.scroll, 1)
        layout = QVBoxLayout(self)
        layout.addWidget(self.search)
        layout.addLayout(middle, 1)
        layout.addLayout(buttons)
        find = QShortcut(QKeySequence(QKeySequence.StandardKey.Find), self)
        find.activated.connect(lambda: (self.search.setFocus(), self.search.selectAll()))
        for section in self.sections.values():
            self._show_advanced(section)
        self._apply_visibility()
        self._check_keys()
        self.nav.setCurrentRow(0)

    # ------------------------------------------------------------ sections and search
    def show_section(self, name: str) -> None:
        """Scroll so that section is at the top (also what the list on the left does)."""
        section = self.sections.get(name)
        if section is not None:
            self.scroll.verticalScrollBar().setValue(section.frame.y())

    def set_advanced_open(self, name: str, open_: bool) -> None:
        section = self.sections[name]
        section.open = open_
        self._show_advanced(section)

    def _show_advanced(self, section: _Section) -> None:
        if section.toggle is None:
            return
        shown = section.open or (self._searching and any(self._row_matches(k) for k in section.advanced_keys))
        section.advanced_box.setVisible(shown)
        changed = sum(1 for k in section.advanced_keys if not self.settings.is_default(k))
        count = len(section.advanced_keys)
        text = f"{'▾' if shown else '▸'}  Advanced ({count})"
        if changed:
            text += f" · {changed} changed"
        section.toggle.setText(text)
        section.toggle.setToolTip("Hide the advanced settings" if shown else "Show the advanced settings")

    def _nav_clicked(self, row: int) -> None:
        if row >= 0 and not self._syncing_nav:
            self.show_section(self.nav.item(row).text())

    _syncing_nav = False

    def _scrolled(self, value: int) -> None:
        """The list on the left follows the scrolling: the section at the top is selected."""
        current = None
        for index, section in enumerate(self.sections.values()):
            if section.frame.isVisible() and section.frame.y() <= value + 30:
                current = index
        if current is not None and current != self.nav.currentRow():
            self._syncing_nav = True
            self.nav.setCurrentRow(current)
            self._syncing_nav = False

    def _row_matches(self, key: str) -> bool:
        words = self.search.text().lower().split()
        if not words:
            return True
        setting = settings_module.BY_KEY[key]
        text = f"{setting.label} {setting.help} {setting.key} {setting.section}".lower()
        if setting.choices:
            text += " " + " ".join(shown for _, shown in setting.choices).lower()
        return all(word in text for word in words)

    def _filter(self, scroll_up: bool = False) -> None:
        words = self.search.text().lower().split()
        self._searching = bool(words)
        found = 0
        for index, section in enumerate(self.sections.values()):
            matches = [k for k in section.basic_keys + section.advanced_keys if self._row_matches(k)]
            whole = self._searching and all(word in section.name.lower() for word in words)
            for key in section.basic_keys + section.advanced_keys:
                self._show_row(key, whole or key in matches)
            if section.extras is not None:
                section.extras.setVisible(not self._searching or whole)
            if section.toggle is not None:
                advanced_match = any(k in matches for k in section.advanced_keys)
                section.toggle.setVisible(not self._searching or whole or advanced_match)
            self._show_advanced(section)
            visible = not self._searching or whole or bool(matches)
            section.frame.setVisible(visible)
            item = self.nav.item(index)
            enabled = Qt.ItemFlag.ItemIsEnabled
            item.setFlags(item.flags() | enabled if visible else item.flags() & ~enabled)
            found += len(matches) if self._searching else 0
        self.nothing.setVisible(self._searching and found == 0)
        self.nothing.setText(f"No setting matches “{self.search.text().strip()}”." if self._searching else "")
        if scroll_up:
            self.scroll.verticalScrollBar().setValue(0)

    def _show_row(self, key: str, visible: bool) -> None:
        inline = bool(self.settings.get("ui.settings_help_inline"))
        for widget in self.rows.get(key, []):
            widget.setVisible(visible and (inline or widget not in self.inline_help))

    def keyPressEvent(self, event) -> None:  # noqa: N802 (Qt naming)
        """Esc first empties the search box, then closes."""
        if event.key() == Qt.Key.Key_Escape and self.search.text():
            self.search.clear()
            return
        super().keyPressEvent(event)

    # ------------------------------------------------------------ building rows
    def _add_row(self, grid: QGridLayout, row: int, setting: Setting, indent: int = 0) -> int:
        """Adds one setting (two grid rows if it has an explanation). Returns the next free row.
        ``indent``: how far right this grid starts (the Advanced part), so the controls still line up."""
        label_box = QWidget()
        label_box.setFixedWidth(LABEL_WIDTH - indent)
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
        if setting.kind != "bool":
            editor.setMaximumWidth(EDITOR_WIDTH + (100 if setting.kind in ("path", "keys") else 0))
        reset = QToolButton()
        reset.setObjectName("ResetButton")
        reset.setText("↺")
        reset.setToolTip("Back to default")
        reset.clicked.connect(lambda _=False, k=setting.key: self._reset(k))
        grid.addWidget(label_box, row, 0, Qt.AlignmentFlag.AlignVCenter)
        grid.addWidget(editor, row, 1, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
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
        if kind == "secret":
            line.setEchoMode(QLineEdit.EchoMode.PasswordEchoOnEdit)
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

    def _apply_visibility(self) -> None:
        """The explanations under the settings (or not), and what the search box asks for."""
        self._filter()

    def _update_note(self) -> None:
        for section in self.sections.values():
            self._show_advanced(section)

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
    """Buttons for the log and the startup checks (in Updates & help)."""
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
    """Picture controls for the Profile section."""
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
    """Version info and buttons for the updates (in Updates & help)."""
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
    """Your ID, and the people you blocked (with Unblock), for the Rooms & friends section."""
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
