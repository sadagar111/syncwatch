"""One person in a three-app room test: the real app with a real video player, driven by a script.

    python tests/room_gui_harness.py host|marek|kuba <film> <log file> <relay url> <code file> [<screenshots dir>]

The host opens its room, writes the room code to <code file> and lets in
whoever asks; the others wait for that file and join with the code. Every
50 ms each app logs (wall clock, film position, playing) as JSON lines, so
tests/gui_checks.py can compare the apps' positions at the same moments.
"""
import json
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

ROLE, FILM, LOG, RELAY, CODE_FILE = (sys.argv[1], Path(sys.argv[2]), Path(sys.argv[3]), sys.argv[4],
                                     Path(sys.argv[5]))
SHOTS = Path(sys.argv[6]) if len(sys.argv) > 6 else None
NAMES = {"host": "Alex", "marek": "Marek", "kuba": "Kuba"}
data_dir = Path(tempfile.mkdtemp(prefix=f"sw-{ROLE}-"))
os.environ["SYNCWATCH_DATA_DIR"] = str(data_dir)
os.environ["SYNCWATCH_RELAY"] = RELAY
profile = data_dir / "profiles" / ROLE
profile.mkdir(parents=True)
(profile / "settings.json").write_text(json.dumps({"_version": 1, "values": {
    "profile.name": NAMES[ROLE], "ui.startup_checks": "never", "ui.video_selftest": False,
    "playback.resume": False, "sync.gap_refresh_s": 1, "sync.countdown_s": 2,
    "sync.gap_reference": "host"}}), encoding="utf-8")

from PySide6.QtGui import QPainter  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from syncwatch import app as app_module  # noqa: E402


def window():
    from syncwatch.ui.main_window import MainWindow
    return next(w for w in QApplication.topLevelWidgets() if isinstance(w, MainWindow))


def shot(name):
    if SHOTS is None:
        return
    SHOTS.mkdir(parents=True, exist_ok=True)
    w = window()
    image = w.grab()
    if w.overlay.isVisible():
        painter = QPainter(image)
        painter.drawPixmap(w.mapFromGlobal(w.overlay.geometry().topLeft()), w.overlay.grab())
        painter.end()
    image.save(str(SHOTS / f"room_{ROLE}_{name}.png"))


# Every event name is unique, so tests/gui_checks.py can measure the gaps around them.
# Timed for the 40-second film in tests/media.
SCRIPTS = {
    "host": [(1, "host"), (8, "play"), (12, "shot_playing"), (14, "pause"), (17, "seek5"), (19, "play2"),
             (34, "syncup"), (44, "skipspam"), (52, "end")],
    "marek": [(3, "join"), (12, "shot_playing"), (24, "drift"), (29, "report_gap"), (30, "syncme"),
              (40, "friend_pause"), (42, "friend_play"), (43, "friend_pause_cooldown"), (52, "end")],
    "kuba": [(3.5, "join"), (12, "shot_playing"), (52, "end")],
}


def scripted_exec(self):
    w = window()
    w.open_film(FILM)
    script = list(SCRIPTS[ROLE])
    start = time.monotonic()
    out = LOG.open("w")
    problems = []
    from syncwatch import errors
    errors.set_handler(lambda r: problems.append(r.code))
    while script:
        now = time.monotonic() - start
        c = w.controller
        if ROLE == "host" and c.session.waiting:
            c.approve_all()
            out.write(json.dumps({"event": "approved", "t": time.time()}) + "\n")
        if ROLE == "host" and c.session.code is not None and not CODE_FILE.exists():
            CODE_FILE.write_text(c.session.code.text(), encoding="utf-8")
        while script and now >= script[0][0]:
            _, action = script.pop(0)
            if action == "host":
                w._host()
            elif action == "join":
                deadline = time.monotonic() + 20
                while not CODE_FILE.exists() and time.monotonic() < deadline:
                    QApplication.processEvents()
                    time.sleep(0.05)
                w._join(CODE_FILE.read_text(encoding="utf-8"))
            elif action in ("play", "play2"):
                c.play()
            elif action == "pause":
                c.pause()
            elif action == "seek5":
                c.seek(5_000)
            elif action == "drift":
                w.player.seek(w.player.position() + 2500)  # behind the app's back: simulated drift
            elif action == "report_gap":
                out.write(json.dumps({"event": "gap_shown", "gap_ms": c.gap_ms(), "text": w._gap_text}) + "\n")
            elif action == "syncme":
                c.sync_me()
            elif action == "syncup":
                c.sync_up()
            elif action == "skipspam":
                for _ in range(5):
                    c.skip(10_000)
                    QApplication.processEvents()
                    time.sleep(0.05)
            elif action in ("friend_pause", "friend_pause_cooldown"):
                c.pause()
            elif action == "friend_play":
                c.play()
            elif action.startswith("shot_"):
                QApplication.processEvents()
                shot(action[5:])
            elif action == "end":
                script.clear()
            out.write(json.dumps({"event": f"{ROLE}:{action}" if ROLE != "host" else action, "t": time.time()})
                      + "\n")
        QApplication.processEvents()
        out.write(json.dumps({"t": time.time(), "pos": w.player.position(), "playing": w.player.is_playing(),
                              "in_room": c.connected, "people": len(c.peers)}) + "\n")
        time.sleep(0.05)
    out.write(json.dumps({"event": "problems", "codes": problems,
                          "activity": [w.sidebar.activity.list.item(i).text()
                                       for i in range(w.sidebar.activity.list.count())]}) + "\n")
    out.close()
    for _ in range(5):  # let queued updates (new rows in the panels) show before the picture
        QApplication.processEvents()
        time.sleep(0.05)
    shot("end")
    for widget in QApplication.topLevelWidgets():
        widget.close()
    return 0


QApplication.exec = scripted_exec
sys.exit(app_module.main(["--profile", ROLE]))
