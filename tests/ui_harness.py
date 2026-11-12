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
import faulthandler
faulthandler.enable(all_threads=sys.platform != "win32")   # a crash inside Qt or mpv prints where every thread was

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
from PySide6.QtGui import QColor, QPainter, QPixmap  # noqa: E402
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


def spin_until(seconds, until):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if until():
            return True
        spin(0.1)
    return bool(until())


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


def open_menu_checks(window):
    """The 📂 button (0.5.2) is a menu: your films, a link, a .torrent file. The link window and the
    file picker wait for an answer, so here they only note that they were asked for."""
    from unittest import mock
    from PySide6.QtWidgets import QFileDialog
    from syncwatch.ui import main_window as main_window_module
    asked = []
    menu = window.controls.open_menu
    actions = {action.text().split("\t")[0]: action for action in menu.actions()}
    with mock.patch.object(main_window_module.LinkDialog, "exec", lambda self: asked.append("link") or 0), \
            mock.patch.object(QFileDialog, "getOpenFileName", lambda *a, **k: asked.append("torrent") or ("", "")):
        for text in ("🔗  Open a link…", "📁  Open a .torrent file…", "📂  Your films…"):
            if text in actions:
                actions[text].trigger()
                spin(0.3)
    library = next((w for w in QApplication.topLevelWidgets() if w.isVisible() and w.windowTitle() == "Library"), None)
    checks = {"menu has films, link, .torrent": list(actions) == ["📂  Your films…", "🔗  Open a link…",
                                                                   "📁  Open a .torrent file…"],
              "the menu shows the shortcuts": [a.text().split("\t")[-1] for a in menu.actions()][:2] == ["Ctrl+O", "Ctrl+L"],
              "Open a link asks for one": "link" in asked,
              "Open a .torrent file picks one": "torrent" in asked,
              "Your films opens the library": library is not None}
    if library is not None:
        shot_widget(library, "02b_library_window")
        library.close()
    print("OPEN MENU CHECKS:", checks)
    FAILED.extend(name for name, ok in checks.items() if not ok)


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
    # the lobby (0.5.2): what SyncWatch starts with
    lobby = {"starts in the lobby": window.stack.currentWidget() is window.lobby,
             "no control bar in the lobby": not window.controls.isVisible(),
             "friends on the right": window.sidebar.friends.isVisible(),
             "no room panels in the lobby": not (window.sidebar.room.isVisible() or window.sidebar.people.isVisible()
                                                 or window.sidebar.activity.isVisible()),
             "four cards": len(window.lobby.cards) == 4 and all(card.isVisible() for card in window.lobby.cards)}
    shot("00_lobby")
    window.lobby.alone_button.click()
    spin(0.3)
    lobby["Watch alone shows the start screen"] = window.stack.currentWidget() is window.idle
    lobby["the control bar is back"] = window.controls.isVisible()
    lobby["the room panel is back"] = window.sidebar.room.isVisible()
    print("LOBBY CHECKS:", lobby)
    FAILED.extend(name for name, ok in lobby.items() if not ok)
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
        open_menu_checks(window)
        window.controller.toggle()
        spin(1.0)
        shot("03_playing")
        # pictures over the seek bar (0.5): made in the background, shown while hovering
        made = spin_until(30, lambda: window.previews.file is not None)
        from PySide6.QtTest import QTest
        seek = window.controls.seek
        QTest.mouseMove(seek, QPoint(seek.width() // 3, 20))
        spin(0.3)
        popup = seek._popup
        checks = {"pictures made": made,
                  "picture shown while hovering": popup is not None and popup.isVisible() and popup.pixmap is not None}
        if popup is not None and popup.pixmap is not None:
            popup.grab().save(str(OUT / "03b_seek_picture.png"))
        QTest.mouseMove(window.controls.play, QPoint(5, 5))
        spin(0.2)
        print("PREVIEW CHECKS:", checks)
        FAILED.extend(name for name, ok in checks.items() if not ok)
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
    # chat and reactions (0.5), in the room just opened
    from PySide6.QtTest import QTest
    from PySide6.QtCore import Qt
    activity = window.sidebar.activity
    checks = {"chat box in a room": activity.chat_box.isVisible(),
              "reaction button in a room": window.controls.react.isVisible()}
    window.activateWindow()
    spin(0.2)
    QTest.keyClick(window, Qt.Key.Key_T)
    spin(0.2)
    checks["T goes to the chat box"] = activity.chat_box.hasFocus()
    QTest.keyClicks(activity.chat_box, "hello room 1")       # the digit is typed, not a reaction
    QTest.keyClick(activity.chat_box, Qt.Key.Key_Return)
    spin(0.3)
    last = activity.list.item(activity.list.count() - 1).text() if activity.list.count() else ""
    checks["my message shows"] = last.endswith("💬 You: hello room 1")
    checks["the box is empty again"] = activity.chat_box.text() == ""
    checks["typing a digit sent no reaction"] = len(window.overlay._reactions) == 0
    QTest.keyClick(activity.chat_box, Qt.Key.Key_Escape)
    spin(0.2)
    QTest.keyClick(window, Qt.Key.Key_7)
    spin(0.2)
    checks["7 sends 🔥"] = [r[0] for r in window.overlay._reactions] == ["🔥"]
    window.controls.reactionChosen.emit("🍿")
    spin(0.3)
    checks["the button sends 🍿"] = [r[0] for r in window.overlay._reactions] == ["🔥", "🍿"]
    frame = QPixmap(window.overlay.size())
    frame.fill(QColor("#30343c"))
    window.overlay.render(frame)
    frame.save(str(OUT / "05b_reactions_over_video.png"))
    shot("05c_chat")
    print("CHAT CHECKS:", checks)
    FAILED.extend(name for name, ok in checks.items() if not ok)
    # 🏠 (0.5.2): back to the lobby; hosting alone, the room just closes (nobody to ask about)
    window.controls.lobby.click()
    spin(0.5)
    checks = {"🏠 goes back to the lobby": window.stack.currentWidget() is window.lobby,
              "and leaves the room": not window.controller.connected and not window.controller.session.is_host,
              "nothing open any more": not window.player.has_media()}
    print("LOBBY BUTTON CHECKS:", checks)
    FAILED.extend(name for name, ok in checks.items() if not ok)
    from syncwatch.ui.dialogs.settings_dialog import SettingsDialog
    dialog = SettingsDialog(window.settings)
    dialog.resize(940, 660)
    dialog.show()
    spin(0.3)
    shot_widget(dialog, "06_settings")
    together = dialog.sections["Watching together"]
    checks = {"advanced folded at first": not together.advanced_box.isVisible(),
              "every section in the list": dialog.nav.count() == len(dialog.sections)}
    dialog.set_advanced_open("Watching together", True)
    dialog.show_section("Watching together")
    spin(0.3)
    checks["advanced unfolds"] = together.advanced_box.isVisible()
    shot_widget(dialog, "07_settings_advanced_together")
    dialog.set_advanced_open("Watching together", False)
    dialog.search.setText("jump")
    spin(0.3)
    shown = [key for key, widgets in dialog.rows.items() if widgets[0].isVisible()]
    checks["search finds advanced settings too"] = "sync.jump_start_ms" in shown and "sync.jump_mode" in shown
    checks["search hides the rest"] = "subs.size_pct" not in shown and not dialog.sections["Subtitles"].frame.isVisible()
    shot_widget(dialog, "07b_settings_search")
    dialog.search.setText("no such setting at all")
    spin(0.2)
    checks["says when nothing matches"] = dialog.nothing.isVisible()
    dialog.search.clear()
    spin(0.2)
    checks["everything back after the search"] = dialog.sections["Subtitles"].frame.isVisible() and \
        not together.advanced_box.isVisible()
    print("SETTINGS CHECKS:", checks)
    FAILED.extend(name for name, ok in checks.items() if not ok)
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
