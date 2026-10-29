"""Runs the real app (offscreen, no visible window) with a scripted session and takes screenshots.

Usage:  QT_QPA_PLATFORM=offscreen python tests/ui_harness.py <script name> <output dir> [film]

The app's normal startup (syncwatch.app.main) runs unchanged; only the final
"wait for the user" loop is replaced by the script below.
"""
import json
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

SCRIPT = sys.argv[1]
OUT = Path(sys.argv[2])
FILM = sys.argv[3] if len(sys.argv) > 3 else None
OUT.mkdir(parents=True, exist_ok=True)

data_dir = Path(tempfile.mkdtemp(prefix="sw-ui-"))
os.environ["SYNCWATCH_DATA_DIR"] = str(data_dir)
from tools.local_relay import LocalRelay  # noqa: E402

RELAY = LocalRelay().start()           # so "Host my room" works without internet
os.environ["SYNCWATCH_RELAY"] = RELAY.url
profile_dir = data_dir / "profiles" / "default"
profile_dir.mkdir(parents=True)
if SCRIPT != "first_launch":
    values = {"profile.name": "Alex", "ui.startup_checks": "always"}
    values.update(json.loads(os.environ.get("SYNCWATCH_TEST_SETTINGS", "{}")))   # e.g. another video drawing mode
    (profile_dir / "settings.json").write_text(json.dumps({"_version": 1, "values": values}), encoding="utf-8")

from PySide6.QtCore import QPoint  # noqa: E402
from PySide6.QtGui import QPainter, QPixmap  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from syncwatch import app as app_module  # noqa: E402


def spin(seconds):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        QApplication.processEvents()
        time.sleep(0.01)


def main_window():
    from syncwatch.ui.main_window import MainWindow
    for widget in QApplication.topLevelWidgets():
        if isinstance(widget, MainWindow):
            return widget
    return None


def shot(name):
    """Screenshot of the main window with the overlay composited on top (as it looks on screen)."""
    window = main_window()
    image = window.grab()
    overlay = window.overlay
    if overlay.isVisible():
        painter = QPainter(image)
        offset = window.mapFromGlobal(overlay.geometry().topLeft())
        painter.drawPixmap(offset, overlay.grab())
        painter.end()
    image.save(str(OUT / f"{name}.png"))


def shot_widget(widget, name):
    widget.grab().save(str(OUT / f"{name}.png"))


FAILED = []


def scripted_exec(self):
    errors_seen = []
    from syncwatch import errors
    original = errors._handler

    def record(report):
        errors_seen.append(report.code)
        if original:
            original(report)

    errors.set_handler(record)
    try:
        run_script(errors_seen)
    except Exception:
        import traceback
        traceback.print_exc()
        print("SCRIPT FAILED")
        FAILED.append("exception")
    print("ERRORS SEEN:", errors_seen)
    for widget in QApplication.topLevelWidgets():
        widget.close()
    return 0


def run_script(errors_seen):
    spin(1.5)
    window = main_window()
    for widget in QApplication.topLevelWidgets():
        if widget.isVisible() and widget is not window and widget.windowTitle():
            print("open window:", widget.windowTitle())
            shot_widget(widget, "dialog_" + widget.windowTitle().replace(" ", "_").replace(":", "")[:40])
    area = QApplication.primaryScreen().availableGeometry()
    widest = max(min(1280, area.width()), window.minimumSizeHint().width())
    checks = {"window opens no wider than asked": window.width() <= widest,
              "room buttons hidden outside a room": not window.controls.sync_up.isVisible()}
    window.resize(1280, 760)
    spin(0.3)
    idle = window.idle
    card = idle.library.parentWidget()
    middle = card.mapTo(idle, QPoint(card.width() // 2, 0)).x()
    checks["library card in the middle"] = abs(middle - idle.width() / 2) <= 2
    print("WINDOW CHECKS:", checks, f"(window {window.width()} px, screen {area.width()} px, "
          f"card middle {middle} of {idle.width()})")
    FAILED.extend(name for name, ok in checks.items() if not ok)
    print("PLAYER:", window.player.describe())
    shot("01_start")
    if FILM:
        window.open_film(Path(FILM))
        spin(2.0)
        window.player.seek(20_000)
        spin(1.5)
        shot("02_film_paused")
        window.controller.toggle()
        spin(1.0)
        shot("03_playing")
        print("position after play:", int(window.player.position()), "playing:", window.player.is_playing())
        window.controller.toggle()
        spin(0.5)
        window.toggle_help()
        spin(0.3)
        shot("04_help")
        window.toggle_help()
        from PySide6.QtTest import QTest
        from PySide6.QtCore import Qt
        window.activateWindow()
        spin(0.2)
        checks = {}
        before = window.player.is_playing()
        QTest.keyClick(window, Qt.Key.Key_Space); spin(0.4)
        checks["space toggles play"] = window.player.is_playing() != before
        QTest.keyClick(window, Qt.Key.Key_Space); spin(0.3)
        pos = window.player.position()
        QTest.keyClick(window, Qt.Key.Key_Right); spin(0.6)
        checks["right skips 10 s"] = abs(window.player.position() - pos - 10_000) < 600
        delay = window.subtitles.delay_ms
        QTest.keyClick(window, Qt.Key.Key_X); spin(0.1)
        checks["X delays subtitles"] = window.subtitles.delay_ms == delay + 100
        QTest.keyClick(window, Qt.Key.Key_M); spin(0.1)
        checks["M mutes"] = window.settings.get("playback.muted") is True
        QTest.keyClick(window, Qt.Key.Key_M); spin(0.1)
        toggles = []
        window.showFullScreen = lambda: (toggles.append(1), type(window).showFullScreen(window))
        QTest.keyClick(window, Qt.Key.Key_F)
        deadline = time.monotonic() + 2.0  # some platforms take a moment to change the window state
        while not window.isFullScreen() and time.monotonic() < deadline:
            spin(0.1)
        del window.showFullScreen
        checks["F fullscreen"] = window.isFullScreen()
        if not checks["F fullscreen"]:
            print(f"fullscreen diagnostics: showFullScreen_calls={len(toggles)} state={window.windowState()!r} "
                  f"focus={QApplication.focusWidget()!r} platform={QApplication.platformName()}")
        QTest.keyClick(window, Qt.Key.Key_Escape); spin(0.4)
        checks["Esc leaves fullscreen"] = not window.isFullScreen()
        vol = window.settings.get("playback.volume")
        QTest.keyClick(window, Qt.Key.Key_Down); spin(0.1)
        checks["Down lowers volume"] = window.settings.get("playback.volume") == vol - 5
        print("KEY CHECKS:", checks)
        FAILED.extend(name for name, ok in checks.items() if not ok)
        window._build_subs_menu()
        print("subtitle sources:", [s.label for s in window.subtitles.sources],
              "current:", window.subtitles.current_id, "status:", window.subtitles.status)
    window.sidebar.room.hostClicked.emit()
    spin(1.0)
    shot("05_hosting")
    checks = {"hosting": window.controller.session.is_host,
              "room code shown": window.sidebar.room.code_label.text().startswith("Room code: SW-"),
              "Sync up shown in a room": window.controls.sync_up.isVisible()}
    print("ROOM CHECKS:", checks)
    FAILED.extend(name for name, ok in checks.items() if not ok)
    from syncwatch.ui.dialogs.settings_dialog import SettingsDialog
    dialog = SettingsDialog(window.settings)
    dialog.resize(680, 600)
    dialog.show()
    spin(0.3)
    shot_widget(dialog, "06_settings_basic")
    dialog.advanced.setChecked(True)
    dialog.tabs.setCurrentIndex(3)
    spin(0.3)
    shot_widget(dialog, "07_settings_advanced_sync")
    dialog.close()
    from syncwatch import errors
    errors.report("SW-403", "SSLCertVerificationError: certificate verify failed")
    spin(0.5)
    for widget in QApplication.topLevelWidgets():
        if widget.isVisible() and "SW-403" in widget.windowTitle():
            shot_widget(widget, "08_error_popup")


QApplication.exec = scripted_exec
app_module.main([])
if FAILED:
    print("UI HARNESS FAILED:", FAILED)
    sys.exit(1)
print("UI HARNESS OK")
sys.exit(0)
