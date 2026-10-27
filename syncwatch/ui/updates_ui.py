"""Connects the update manager to the window: background checks, banner, dialog, restart.

Network work runs on background threads; results come back to the window
through Qt signals, which Qt delivers on the main thread.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Optional

from PySide6.QtCore import QObject, QTimer, Signal

from .. import __version__, errors
from ..update import github, install
from ..update.manager import CheckResult, UpdateManager
from .dialogs.update_dialog import UpdateDialog

log = logging.getLogger("syncwatch.update")

HEALTHY_AFTER_MS = 15_000
FIRST_CHECK_DELAY_MS = 5_000


class UpdateController(QObject):
    _checked = Signal(object, bool)
    _progress = Signal(int, int, float, object)
    _prepared = Signal(object)

    def __init__(self, window, manager: Optional[UpdateManager] = None):
        super().__init__(window)
        self.window = window
        self.settings = window.settings
        self.manager = manager or UpdateManager()
        self.result: Optional[CheckResult] = None
        self.dialog: Optional[UpdateDialog] = None
        self.cancel_event: Optional[threading.Event] = None
        self.checking = False
        self.last_text = ""
        self._checked.connect(self._on_checked)
        self._progress.connect(self._on_progress)
        self._prepared.connect(self._on_prepared)

    # ------------------------------------------------------------ startup
    def startup(self) -> None:
        news = self.manager.startup_news()
        if news.updated_to:
            self.window.activity(f"Updated to SyncWatch {news.updated_to} (from {news.updated_from})", "you")
        for code in news.problems:
            if code == "SW-707":
                state = {"version": news.rolled_back_from, "to": news.rolled_back_to}
                errors.report("SW-714" if self.manager.layout.read_state().get("rolled_back", {}).get("manual")
                              else "SW-707", **state)
            elif code == "SW-706":
                errors.report("SW-706", news.apply_error)
        QTimer.singleShot(HEALTHY_AFTER_MS, self.manager.mark_healthy)
        if self.manager.availability().status == "off":
            return
        if self.settings.get("updates.auto_check"):
            hours = float(self.settings.get("updates.check_hours"))
            if time.time() - float(self.settings.get("updates.last_check")) >= hours * 3600:
                QTimer.singleShot(FIRST_CHECK_DELAY_MS, lambda: self.check(manual=False))
            else:
                log.info("skipping update check: last check was less than %.0f h ago", hours)

    # ------------------------------------------------------------ checking
    def check(self, manual: bool) -> None:
        if self.checking:
            return
        self.checking = True
        self.last_text = "Checking…"
        include_test = bool(self.settings.get("updates.test_versions"))
        skip = self.settings.get("updates.skip_version") or self.manager.layout.read_state().get("auto_skip", "")

        def work() -> None:
            try:
                result = self.manager.check(include_test, skip, manual)
            except Exception as exc:  # never let a check crash the app
                log.exception("update check failed")
                result = CheckResult("error", code="SW-701", technical=errors.describe_exception(exc))
            self._checked.emit(result, manual)

        threading.Thread(target=work, daemon=True, name="sw-update-check").start()

    def _on_checked(self, result: CheckResult, manual: bool) -> None:
        self.checking = False
        if result.status != "error" or result.code == "SW-703":
            self.settings.set("updates.last_check", time.time())
        if result.status == "update":
            self.result = result
            self.last_text = f"Version {result.manifest.version} is available."
            if not self.manager.layout.installed:
                self.last_text += " (Update this copy with git pull.)"
                return
            self.window.sidebar.show_update(str(result.manifest.version))
            self.window.activity(f"SyncWatch {result.manifest.version} is available", "info")
            if manual:
                self.open_dialog()
        elif result.status in ("current", "skipped", "none_published"):
            newest = f" (newest published: {result.manifest.version})" if result.manifest else ""
            self.last_text = f"You have the newest version{newest}."
            if result.status == "skipped":
                self.last_text = f"Version {result.manifest.version} is available, but you chose to skip it."
            if manual:
                self.window.activity(self.last_text, "info")
        elif result.status == "off":
            self.last_text = errors.problem(result.code).title
            if manual:
                errors.report(result.code, result.technical, popup=True)
        else:
            self.last_text = errors.problem(result.code).title
            errors.report(result.code, result.technical, popup=True if manual else None)
        if self.window.settings_dialog_refresh:
            self.window.settings_dialog_refresh()

    # ------------------------------------------------------------ updating
    def open_dialog(self) -> None:
        if self.result is None or self.result.status != "update":
            return
        if self.dialog is not None and self.dialog.isVisible():
            self.dialog.raise_()
            return
        peer = self.window.controller.peer
        self.dialog = UpdateDialog(self.result, peer.name if peer and self.window.controller.connected else "",
                                   self.window)
        self.dialog.updateClicked.connect(self._start_download)
        self.dialog.skipClicked.connect(self._skip)
        self.dialog.cancelClicked.connect(self._cancel)
        self.dialog.restartClicked.connect(self.window.request_restart)
        if self.manager.staged() == str(self.result.manifest.version):
            self.dialog.show_ready()
        self.dialog.show()

    def _skip(self) -> None:
        if self.result is not None:
            self.settings.set("updates.skip_version", str(self.result.manifest.version))
            self.window.sidebar.show_update("")

    def _start_download(self) -> None:
        if self.result is None or self.cancel_event is not None:
            return
        self.dialog.show_progress()
        self.cancel_event = threading.Event()
        result, cancel = self.result, self.cancel_event

        def work() -> None:
            try:
                self.manager.prepare(result, lambda *a: self._progress.emit(*a), cancel)
                outcome = None
            except Exception as exc:
                outcome = exc
            self._prepared.emit(outcome)

        threading.Thread(target=work, daemon=True, name="sw-update-download").start()

    def _cancel(self) -> None:
        if self.cancel_event is not None:
            self.cancel_event.set()

    def _on_progress(self, done: int, total: int, speed: float, eta) -> None:
        if self.dialog is not None:
            self.dialog.set_progress(done, total, speed, eta)

    def _on_prepared(self, outcome) -> None:
        self.cancel_event = None
        if outcome is None:
            self.window.activity(f"SyncWatch {self.result.manifest.version} is downloaded and ready", "you")
            self.window.sidebar.show_update(str(self.result.manifest.version), ready=True)
            if self.dialog is not None:
                self.dialog.show_ready()
            return
        if self.dialog is not None:
            self.dialog.show_info()
        if isinstance(outcome, github.Cancelled):
            self.window.activity("Update download cancelled. It continues where it stopped next time.", "info")
        elif isinstance(outcome, github.ChecksumError):
            errors.report("SW-705", str(outcome))
        elif isinstance(outcome, github.NetError):
            errors.report("SW-704" if outcome.code == "SW-701" else outcome.code, outcome.technical)
        elif isinstance(outcome, install.InstallError):
            errors.report(outcome.code, outcome.technical)
        else:
            errors.report("SW-706", exc=outcome)

    # ------------------------------------------------------------ settings tab
    def describe(self) -> str:
        layout = self.manager.layout
        mode = "installed copy" if layout.installed else "source-code copy (update with git pull)"
        lines = [f"This is SyncWatch {__version__}, {mode}."]
        staged = self.manager.staged()
        if staged:
            lines.append(f"Version {staged} is downloaded and starts the next time you open SyncWatch.")
        last = float(self.settings.get("updates.last_check"))
        if last:
            lines.append("Last checked " + time.strftime("%d %b %H:%M", time.localtime(last)) + ".")
        if self.last_text:
            lines.append(self.last_text)
        availability = self.manager.availability()
        if availability.status == "off" and availability.code == "SW-711":
            lines.append("Updates aren't set up in this copy yet.")
        return "\n".join(lines)

    def can_go_back(self) -> str:
        layout = self.manager.layout
        return layout.previous_version() if layout.installed and layout.has_previous() else ""

    def go_back(self) -> None:
        if self.manager.request_rollback():
            self.window.activity(f"Going back to version {self.can_go_back()} at the next start", "you")
            self.window.request_restart()
