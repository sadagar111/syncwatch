"""One person in a three-app room test: the real app with a real video player, driven by a script.

    python tests/room_gui_harness.py host|marek|kuba <film> <log file> <relay url> <code file> [<screenshots dir>]

The three apps wait for each other, then start their scripts at the same moment.
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
import faulthandler
faulthandler.enable(all_threads=sys.platform != "win32")   # a crash inside Qt or mpv prints where every thread was

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
    "sync.gap_reference": "host",
    # (the torrent offer at the end) Marek is asked first (the default) and then streams with nothing
    # reaching the internet; Kuba has said no in Settings
    **({"torrents.dht": False, "torrents.incoming": False} if ROLE == "marek" else {}),
    **({"torrents.follow_host": "no"} if ROLE == "kuba" else {})}}), encoding="utf-8")

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
             (20.5, "jump_playing"), (34, "syncup"), (44, "skipspam"), (45, "vote_suggest"), (49.5, "vote_close"),
             (51, "shot_vote"), (51.5, "seek5"), (52.5, "ready_ask"), (61, "ready_seen"), (62, "torrent_film"),
             (68.5, "end")],
    "marek": [(3, "join"), (12, "shot_playing"), (24, "drift"), (29, "report_gap"), (30, "syncme"),
              (40, "friend_pause"), (42, "friend_play"), (43, "friend_pause_cooldown"), (45.5, "chat"),
              (47, "vote_cast"), (51, "shot_vote"), (54.5, "ready_seen"), (55, "ready_yes"), (61, "ready_seen"),
              (65.5, "offer_state"), (66, "offer_accept"), (68, "offer_state2"), (68.5, "end")],
    "kuba": [(3.5, "join"), (12, "shot_playing"), (46, "react"), (47.5, "vote_cast"), (51, "shot_vote"),
             (54.5, "ready_seen"), (56, "ready_yes"), (61, "ready_seen"), (65.5, "offer_state"), (68.5, "end")],
}
# (after the measured stretches: chat, a reaction and a film vote, through the windows' own buttons and boxes;
# back to 0:05 and the host's ready check, answered with the members' own buttons; then the host streams a
# torrent, which nobody has, so nothing downloads)
OFFER_HASH = "5a" * 20
OFFER_MAGNET = f"magnet:?xt=urn:btih:{OFFER_HASH}&dn=Torrent%20Test%20Film.mkv"


def listen(w, out) -> None:
    """Log what reaches this app: chat messages, reactions, the vote's winner (gui_checks compares)."""
    for name in ("chat_message", "reaction", "vote_won"):
        original = getattr(w, name, None)
        if original is None:
            continue

        def hooked(*args, _name=name, _original=original):
            shown = [a if a is None or isinstance(a, (str, int, float, bool)) else getattr(a, "title", str(a))
                     for a in args]                          # (a vote's Choice: its title)
            out.write(json.dumps({"event": f"got_{_name}", "args": shown, "t": time.time()}) + "\n")
            return _original(*args)
        setattr(w, name, hooked)


def wait_for_the_others() -> None:
    """Start all three scripts at the same moment, however long each app took to open.

    The scripts' times only line up if they start together; on a slow PC one app can be
    ready seconds after another (then, say, Marek's "friend_pause" at 40 s would come after
    the host's jumps at 44 s)."""
    folder = CODE_FILE.parent
    mine = folder / f"room-ready-{ROLE}"
    tmp = folder / f"room-ready-{ROLE}.tmp"
    tmp.write_text(repr(time.time()), encoding="utf-8")
    os.replace(tmp, mine)   # all at once, so nobody reads half a number
    everyone = [folder / f"room-ready-{role}" for role in NAMES]
    deadline = time.monotonic() + 120
    while not all(p.exists() for p in everyone) and time.monotonic() < deadline:
        QApplication.processEvents()
        time.sleep(0.05)
    times = []
    for path in everyone:
        try:
            times.append(float(path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            pass
    go = max(times, default=time.time()) + 1.0   # the same moment for all three (same PC clock)
    while time.time() < go:
        QApplication.processEvents()
        time.sleep(0.01)


def scripted_exec(self):
    w = window()
    w.open_film(FILM)
    wait_for_the_others()
    script = list(SCRIPTS[ROLE])
    start = time.monotonic()
    out = LOG.open("w")
    listen(w, out)
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
            elif action == "jump_playing":
                c.seek(9_000)  # while playing: everyone pauses there and starts together
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
            elif action == "vote_suggest":
                c.votes.suggest("The Matrix", 1999)
                c.votes.suggest("Inception", 2010)
            elif action == "chat":
                w.sidebar.activity.chat_box.setText("hello from Marek")
                w.sidebar.activity.chat_box.returnPressed.emit()
            elif action == "react":
                w.controls.reactionChosen.emit("🍿")
            elif action == "vote_cast":
                choice = next((ch for ch in c.votes.vote.choices if ch.title == "Inception"), None)
                if choice is not None:
                    w.sidebar.vote.voteRequested.emit(choice.key)
            elif action == "vote_close":
                w.sidebar.vote.close_button.click()
            elif action == "torrent_film":
                c.set_film("Torrent Test Film.mkv", 734_003_200, 0, magnet=OFFER_MAGNET)
            elif action in ("offer_state", "offer_state2"):
                banner = w.sidebar.share_banner
                out.write(json.dumps({"event": f"{ROLE}:{action}:seen", "t": time.time(),
                                      "banner": "" if banner.isHidden() else banner.button.text(),
                                      "streaming": bool(w.torrents.active and w.torrents.hash == OFFER_HASH)}) + "\n")
                if action == "offer_state2":
                    w.torrents.stop()
            elif action == "offer_accept":
                w.sidebar.share_banner.button.click()
            elif action in ("ready_ask", "ready_yes"):
                w.sidebar.people.ready_button.click()
            elif action == "ready_seen":
                people = w.sidebar.people
                out.write(json.dumps({"event": f"{ROLE}:ready:seen", "t": time.time(),
                                      "line": "" if people.ready_line.isHidden() else people.ready_line.text(),
                                      "button": "" if people.ready_button.isHidden() else people.ready_button.text(),
                                      "playing": bool(w.player.is_playing())}) + "\n")
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
