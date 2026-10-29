"""Starting SyncWatch, step by step.

    1. Profile folder and logging (so everything after this is recorded)
    2. Crash catcher: Python errors become SW-901/902 popups instead of a silent
       exit; hard crashes inside Qt/video code are written to a file and
       reported (SW-990) the next time you start
    3. One window per profile (SW-108)
    4. Settings, film memory, theme (damaged files fall back to defaults), this PC's
       identity and the rooms file (room code, friends)
    5. Startup checks (app files, video, network) and the invisible video test
    6. First-time setup (your name and picture), then the main window
"""
from __future__ import annotations

import argparse
import faulthandler
import getpass
import logging
import os
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Optional

from . import APP_NAME, __version__, errors, instance, log as log_module, paths, theme as theme_module
from .filmstate import FilmMemory
from .settings import SCHEMA, Settings

log = logging.getLogger("syncwatch.app")
MAX_AVATAR_FILE = 200 * 1024


@dataclass
class AppContext:
    profile: paths.Profile
    settings: Settings
    films: FilmMemory
    theme: theme_module.Theme
    avatar: Optional[bytes]
    data_ok: bool
    data_error: str
    startup_problems: List[str]
    rooms: object = None
    check_results: list = field(default_factory=list)
    restart: bool = False
    rerun_checks: Callable[[], None] = lambda: None
    build_report: Callable[[object], str] = lambda window: ""


def parse_args(argv: Optional[List[str]]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="syncwatch", description="Watch films in sync with friends.")
    parser.add_argument("--profile", default="default",
                        help="use a separate set of settings, e.g. to test two windows on one PC")
    parser.add_argument("--debug", action="store_true", help="detailed logging")
    parser.add_argument("--reset-settings", action="store_true", help="start with default settings")
    parser.add_argument("--film", help="open this film at startup")
    return parser.parse_args(argv)


def _install_crash_handlers(profile: paths.Profile) -> Optional[str]:
    """Returns the text of a crash from last time, if there was one."""
    previous = None
    try:
        if profile.crash_file.exists() and profile.crash_file.stat().st_size > 0:
            previous = profile.crash_file.read_text(encoding="utf-8", errors="replace")[-20_000:]
        handle = open(profile.crash_file, "w", encoding="utf-8")
        faulthandler.enable(file=handle, all_threads=True)
        _install_crash_handlers.handle = handle  # type: ignore[attr-defined]
    except OSError:
        pass

    def on_exception(kind, value, traceback):
        if issubclass(kind, KeyboardInterrupt):
            sys.__excepthook__(kind, value, traceback)
            return
        errors.report("SW-901", exc=value)

    def on_thread_exception(args):
        if args.exc_type is SystemExit:
            return
        errors.report("SW-902", f"thread {args.thread.name if args.thread else '?'}", exc=args.exc_value)

    sys.excepthook = on_exception
    threading.excepthook = on_thread_exception
    return previous


def _clear_crash_file(profile: paths.Profile) -> None:
    handle = getattr(_install_crash_handlers, "handle", None)
    try:
        faulthandler.disable()
        if handle is not None:
            handle.close()
        profile.crash_file.unlink()
    except OSError:
        pass


def _qt_messages(mode, context, message) -> None:
    from PySide6.QtCore import QtMsgType
    qt_log = logging.getLogger("syncwatch.qt")
    if mode == QtMsgType.QtFatalMsg:
        errors.report("SW-903", message)
    elif mode == QtMsgType.QtCriticalMsg:
        qt_log.error(message)
    elif mode == QtMsgType.QtWarningMsg:
        qt_log.warning(message)
    else:
        qt_log.debug(message)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    from PySide6.QtCore import QTimer, qInstallMessageHandler
    from PySide6.QtWidgets import QApplication, QDialog

    app = QApplication(sys.argv[:1])
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(__version__)

    # ---- 1. profile and logging ------------------------------------------------------
    if not paths.valid_profile_name(args.profile):
        _fatal_popup("SW-109", f"profile {args.profile!r}")
        return 2
    profile = paths.Profile(args.profile)
    data_ok, data_error = True, ""
    try:
        profile.ensure()
    except OSError as exc:
        data_ok, data_error = False, errors.describe_exception(exc)
    log_module.setup(profile.logs_dir if data_ok else None, debug=args.debug)
    log.info("SyncWatch %s starting (profile %s) | %s", __version__, profile.name, errors.system_summary())

    # ---- 2. crash catcher -------------------------------------------------------------
    previous_crash = _install_crash_handlers(profile) if data_ok else None
    qInstallMessageHandler(_qt_messages)

    # ---- 3. one window per profile ----------------------------------------------------
    if data_ok and not instance.hold(profile.lock_file):
        _fatal_popup("SW-108", f"profile {profile.name} is locked by another SyncWatch")
        return 3

    # ---- 4. settings, film memory, theme ---------------------------------------------
    settings = Settings(profile.settings_file if data_ok else None)
    problems = settings.load()
    if args.reset_settings:
        settings.reset()
    log_module.set_debug(args.debug or bool(settings.get("log.debug")))
    films = FilmMemory(profile.films_file if data_ok else None)
    problems += films.load(paths.OLD_POSITIONS_FILE)
    theme, theme_problems = theme_module.load(paths.ASSETS_DIR)
    problems += theme_problems
    from .ui.style import SyncWatchStyle
    app.setStyle(SyncWatchStyle(theme))
    app.setStyleSheet(theme_module.stylesheet(theme))
    avatar = None
    if profile.avatar_file.is_file() and profile.avatar_file.stat().st_size <= MAX_AVATAR_FILE:
        avatar = profile.avatar_file.read_bytes()

    from .room.context import make_rooms
    rooms = make_rooms(profile.dir if data_ok else None, settings)
    problems += rooms.problems
    context = AppContext(profile, settings, films, theme, avatar, data_ok, data_error, problems, rooms=rooms)

    # ---- 5. startup checks ------------------------------------------------------------
    from .ui import checks
    from .ui.dialogs.startup_dialog import StartupDialog

    def run_checks() -> None:
        context.check_results = (checks.app_checks(data_ok, data_error, problems) + checks.video_checks()
                                 + checks.network_checks(rooms.default_relay()))

    run_checks()

    def build_report(window) -> str:
        sections = [("Startup checks", "\n".join(r.line() for r in context.check_results))]
        if window is not None:
            c = window.controller
            s = c.session
            people = ", ".join(f"{p.name}{' (host)' if p.is_host else ''}" for p in c.people()) or "-"
            sections.append(("Room", f"role={s.role} phase={s.phase or '-'} relay={s.relay_address or rooms.default_relay() or '-'} "
                                     f"channel={s.channel_state or '-'} attached={c.attached} "
                                     f"rtt={s.rtt_ms():.0f}ms gap={c.gap_ms()} people=[{people}] "
                                     f"rules={c.effective_rules()} id={rooms.identity.public.short_id} "
                                     f"friends={len(rooms.store.friends)} jumps={c.jump_mode} "
                                     f"jump_time={c.learned_jump_ms:.0f}ms"))
            player = window.player
            sections.append(("Film", f"{player.path.name if player.path else '-'} duration={player.duration()} "
                                     f"position={int(player.position())} playing={player.is_playing()} "
                                     f"ready={player.is_ready()} subtitles={window.subtitles.current_id.split(':')[0]} "
                                     f"delay={window.subtitles.delay_ms}"))
        changed = [f"{s.key} = {settings.get(s.key)!r}" for s in SCHEMA
                   if not s.hidden and s.key != "profile.name" and not settings.is_default(s.key)]
        sections.append(("Settings changed from defaults", "\n".join(changed) or "(none)"))
        return log_module.build_report(sections)

    context.build_report = build_report
    created: dict = {}  # filled with the main window once it exists
    startup_dialog = StartupDialog(lambda: build_report(created.get("window")))

    def show_checks(force: bool = False) -> None:
        mode = settings.get("ui.startup_checks")
        has_problems = any(r.status != checks.OK for r in context.check_results)
        if force or mode == "always" or (mode == "problems" and has_problems):
            startup_dialog.set_results(context.check_results)
            startup_dialog.show()
            startup_dialog.raise_()

    def rerun_checks() -> None:
        run_checks()
        start_selftest(force_show=True)

    context.rerun_checks = rerun_checks
    selftests: list = []

    def start_selftest(force_show: bool = False) -> None:
        if not settings.get("ui.video_selftest"):
            show_checks(force_show)
            return
        test = checks.VideoSelfTest(app)
        selftests.append(test)

        def done(result) -> None:
            context.check_results = [r for r in context.check_results if r.name != "Test video"] + [result]
            if result.status == checks.FAIL:
                errors.report(result.code, result.technical)
            show_checks(force_show)

        test.finished.connect(done)
        test.start()

    # ---- 6. first-time setup and the main window --------------------------------------
    from .ui.dialogs.setup_dialog import SetupDialog
    from .ui.main_window import MainWindow

    if not settings.get("profile.name"):
        setup = SetupDialog(settings, avatar, first_time=True)
        if setup.exec() == QDialog.DialogCode.Accepted:
            context.avatar = avatar = setup.avatar
            if avatar is not None:
                try:
                    profile.avatar_file.write_bytes(avatar)
                except OSError as exc:
                    errors.report("SW-105", errors.describe_exception(exc))
        if not settings.get("profile.name"):
            settings.set("profile.name", (getpass.getuser() or "Friend")[:32])
    window = MainWindow(context)
    created["window"] = window
    window.show()
    exit_after = os.environ.get("SYNCWATCH_TEST_EXIT_AFTER", "")  # automatic tests only
    if exit_after.replace(".", "", 1).isdigit():
        QTimer.singleShot(int(float(exit_after) * 1000), window.close)

    for code in dict.fromkeys(problems):
        errors.report(code)
    if not data_ok:
        errors.report("SW-105", data_error)
    if previous_crash:
        last_line = previous_crash.strip().splitlines()[0] if previous_crash.strip() else "crash"
        errors.report("SW-990", f"{last_line}\n{previous_crash[-3000:]}")
    QTimer.singleShot(400, start_selftest)
    if args.film:
        QTimer.singleShot(0, lambda: window.open_film(Path(args.film)))
    else:
        QTimer.singleShot(0, window.pick_startup_film)

    exit_code = app.exec()
    settings.save()
    films.save_if_dirty()
    try:
        window.updates.manager.mark_healthy()  # closed normally: this version works
    except Exception:
        log.exception("could not record a healthy start")
    if data_ok:
        _clear_crash_file(profile)
    log.info("SyncWatch closed (exit code %s)%s", exit_code, ", restarting" if context.restart else "")
    if context.restart:
        _restart(args)
    return exit_code


def _restart(args: argparse.Namespace) -> None:
    """Start SyncWatch again (after this one has released its profile lock)."""
    import subprocess
    if instance._held is not None:
        instance._held.release()
    root = paths.INSTALL_ROOT
    extra = ["--profile", args.profile] if args.profile != "default" else []
    if (root / "boot.py").is_file() and paths.APP_DIR.name == "app":
        command = [sys.executable, str(root / "boot.py")] + extra
    else:
        command = [sys.executable, "-m", "syncwatch"] + extra
    flags = 0x00000008 | 0x00000200 if sys.platform == "win32" else 0  # detached, own process group
    try:
        subprocess.Popen(command, cwd=str(root), creationflags=flags, close_fds=True,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError as exc:
        log.error("restart failed: %s", exc)


def _fatal_popup(code: str, technical: str) -> None:
    """An error popup for problems that stop SyncWatch before the main window exists."""
    from .ui.dialogs.error_dialog import ErrorDialog
    dialog = ErrorDialog(errors.build(code, technical))
    dialog.exec()
