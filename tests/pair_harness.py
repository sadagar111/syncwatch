"""One side of a two-app test: the real app with a real video player, driven by a script.

    python tests/pair_harness.py host|friend <film> <log file> <port>

Every 50 ms it logs (wall clock, film position, playing) as JSON lines, so a
second script can compare the two apps' positions at the same moments.
"""
import json
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

ROLE, FILM, LOG, PORT = sys.argv[1], Path(sys.argv[2]), Path(sys.argv[3]), int(sys.argv[4])
data_dir = Path(tempfile.mkdtemp(prefix=f"sw-{ROLE}-"))
os.environ["SYNCWATCH_DATA_DIR"] = str(data_dir)
profile = data_dir / "profiles" / ROLE
profile.mkdir(parents=True)
(profile / "settings.json").write_text(json.dumps({"_version": 1, "values": {
    "profile.name": "Alex" if ROLE == "host" else "Marek", "ui.startup_checks": "never",
    "ui.video_selftest": False, "sync.port": PORT, "playback.resume": False, "sync.gap_refresh_s": 1,
    "sync.countdown_s": 2}}), encoding="utf-8")

from PySide6.QtWidgets import QApplication  # noqa: E402

from syncwatch import app as app_module  # noqa: E402


def window():
    from syncwatch.ui.main_window import MainWindow
    return next(w for w in QApplication.topLevelWidgets() if isinstance(w, MainWindow))


# Every event name is unique, so tests/gui_checks.py can measure the gap between them.
# Timed for the 40-second film in tests/media.
HOST_SCRIPT = [(2, "host"), (8, "play"), (14, "pause"), (17, "seek5"), (19, "play2"),
               (34, "syncup"), (44, "skipspam"), (50, "end")]
FRIEND_SCRIPT = [(4, "join"), (24, "drift"), (29, "report_gap"), (30, "syncme"), (40, "friend_pause"),
                 (42, "friend_play"), (43, "friend_pause_cooldown"), (50, "end")]


def scripted_exec(self):
    w = window()
    w.open_film(FILM)
    script = list(HOST_SCRIPT if ROLE == "host" else FRIEND_SCRIPT)
    start = time.monotonic()
    out = LOG.open("w")
    problems = []
    from syncwatch import errors
    errors.set_handler(lambda r: problems.append(r.code))
    while script:
        now = time.monotonic() - start
        while script and now >= script[0][0]:
            _, action = script.pop(0)
            c = w.controller
            if action == "host":
                w._host()
            elif action == "join":
                w._join(f"127.0.0.1:{PORT}")
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
            elif action == "friend_pause":
                c.pause()
            elif action == "friend_play":
                c.play()
            elif action == "friend_pause_cooldown":
                c.pause()
            elif action == "end":
                script.clear()
            out.write(json.dumps({"event": action, "t": time.time()}) + "\n")
        QApplication.processEvents()
        out.write(json.dumps({"t": time.time(), "pos": w.player.position(), "playing": w.player.is_playing(),
                              "seeks": 0}) + "\n")
        time.sleep(0.05)
    out.write(json.dumps({"event": "problems", "codes": problems,
                          "activity": [w.sidebar.activity.list.item(i).text()
                                       for i in range(w.sidebar.activity.list.count())]}) + "\n")
    out.close()
    for widget in QApplication.topLevelWidgets():
        widget.close()
    return 0


QApplication.exec = scripted_exec
sys.exit(app_module.main(["--profile", ROLE]))
