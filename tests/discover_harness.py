"""Runs the real app (offscreen) through "What should we watch?": ideas, trailers, the film vote.

Usage:  QT_QPA_PLATFORM=offscreen python tests/discover_harness.py <output dir>

TMDB itself isn't used: a stand-in on this PC (tests/fake_tmdb.py) answers instead, and a relay
on this PC carries the room. Everything else is the real thing: the start screen's card, the
Discover window and its background thread, posters, the cache, the vote panel, playing the winner.

1. "Ideas": "For you" from the two films in the library (Interstellar first: both recommend it),
   posters arrive, a film's details (where to watch in NL, the trailer); Trending; Your films; a search.
2. The trailer button plays the trailer (alone: here, through the YouTube part; not resolved here).
3. Host a room, suggest two films, vote, close the vote: the winner is in the library: Play it.
4. Without a key of my own (0.5.1), the relay on this PC asks the stand-in TMDB with its key: a search works.
5. Without a key anywhere, the window says how to get one and "Your films" still lists the library;
   the key never shows up in the logs.
Screenshots go to the output folder. Exit code 0 only if every check passes.
"""
import faulthandler
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

faulthandler.enable()
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "tools"))
OUT = Path(sys.argv[1])
OUT.mkdir(parents=True, exist_ok=True)

from fake_tmdb import KEY, FakeTmdb  # noqa: E402
from local_relay import LocalRelay  # noqa: E402

data_dir = Path(tempfile.mkdtemp(prefix="sw-discover-"))
relay = LocalRelay().start()
os.environ["SYNCWATCH_DATA_DIR"] = str(data_dir)
os.environ["SYNCWATCH_RELAY"] = relay.url
os.environ["no_proxy"] = os.environ["NO_PROXY"] = "127.0.0.1,localhost"
library = data_dir / "library"
library.mkdir()
profile_dir = data_dir / "profiles" / "default"
profile_dir.mkdir(parents=True)
(profile_dir / "settings.json").write_text(json.dumps(
    {"_version": 1, "values": {"profile.name": "Alex", "ui.startup_checks": "never", "ui.video_selftest": False,
                               "paths.movies_dir": str(library), "discover.tmdb_key": KEY,
                               "discover.region": "NL"}}), encoding="utf-8")

from PySide6.QtWidgets import QApplication  # noqa: E402

from syncwatch import app as app_module  # noqa: E402
from syncwatch.discover import tmdb  # noqa: E402
from tests.media_maker import make_film  # noqa: E402

MATRIX, INCEPTION = "The.Matrix.1999.1080p.BluRay.x264.mkv", "Inception (2010).mkv"
make_film(library / MATRIX, seconds=3)
make_film(library / INCEPTION, seconds=3)
server = FakeTmdb().start()
tmdb.API, tmdb.IMAGES = server.api, server.images
CHECKS = {}


def spin(seconds, until=None):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        QApplication.processEvents()
        if until is not None and until():
            return True
        time.sleep(0.01)
    return bool(until()) if until else None


def main_window():
    from syncwatch.ui.main_window import MainWindow
    return next((w for w in QApplication.topLevelWidgets() if isinstance(w, MainWindow)), None)


def shot(widget, name):
    widget.grab().save(str(OUT / f"{name}.png"))


def titles(dialog, tab):
    return [card.title for card in dialog.cards.get(tab, [])]


def run_script():
    window = main_window()
    window.watch_alone()          # past the lobby (0.5.2) to the start screen
    window.resize(1280, 820)
    spin(1.0)
    shot(window, "01_start")
    CHECKS["start screen has the card"] = window.idle.ideas_button.isVisible()

    # 1. ideas
    window.idle.ideas_button.click()
    dialog = window.discover.dialog
    CHECKS["for you appears"] = spin(30, lambda: bool(dialog.cards["you"]))
    first = dialog.cards["you"][0] if dialog.cards["you"] else None
    CHECKS["both films recommend Interstellar"] = bool(first and first.title == "Interstellar"
                                                       and sorted(first.because) == ["Inception", "The Matrix"])
    CHECKS["your films aren't suggested"] = not {"The Matrix", "Inception"} & set(titles(dialog, "you"))
    CHECKS["posters arrive"] = spin(20, lambda: len(window.discover._posters) >= 3)
    dialog.grid.setCurrentRow(0)
    CHECKS["details arrive"] = spin(20, lambda: 157336 in dialog.details)
    spin(0.3)
    CHECKS["where to watch"] = "Netflix" in dialog.where.text() and "(NL)" in dialog.where.text()
    CHECKS["trailer button"] = dialog.trailer_button.isVisibleTo(dialog)
    CHECKS["no vote button alone"] = not dialog.suggest_button.isVisibleTo(dialog)
    shot(dialog, "02_for_you")

    # 2. the trailer (alone: plays here)
    asked = []
    window.open_youtube = lambda ref: asked.append(ref.id)
    dialog.trailer_button.click()
    CHECKS["trailer plays here"] = asked == ["teaserteas1"]

    dialog.show_tab("trending")
    CHECKS["trending"] = spin(20, lambda: titles(dialog, "trending")[:1] == ["Inception"])
    shot(dialog, "03_trending")
    dialog.show_tab("mine")
    spin(0.5)
    CHECKS["your films, matched"] = (titles(dialog, "mine") == ["Inception", "The Matrix"]
                                     and all(card.movie for card in dialog.cards["mine"]))
    dialog.search.setText("batman")
    dialog._search()
    CHECKS["search"] = spin(20, lambda: titles(dialog, "search") == ["Batman Begins"])
    dialog.show_tab("you")
    spin(0.3)
    dialog.surprise.click()
    CHECKS["pick one for me"] = dialog.current is not None and dialog.current.title in titles(dialog, "you")

    # 3. the film vote in a room
    window._host()
    CHECKS["room open"] = spin(20, lambda: window.controller.connected)
    dialog.show_tab("you")
    spin(0.3)
    dialog.select_movie(157336)
    spin(0.3)
    CHECKS["vote button in a room"] = dialog.suggest_button.isVisibleTo(dialog)
    dialog.suggest_button.click()
    votes = window.controller.votes
    CHECKS["suggesting starts the vote"] = spin(5, lambda: votes.vote.open and len(votes.vote.choices) == 1)
    dialog.show_tab("mine")
    spin(0.3)
    matrix_row = titles(dialog, "mine").index("The Matrix")
    dialog.grid.setCurrentRow(matrix_row)
    spin(0.3)
    dialog.suggest_button.click()
    CHECKS["second film"] = spin(5, lambda: len(votes.vote.choices) == 2)
    matrix = next((c for c in votes.vote.choices if c.title == "The Matrix"), None)
    CHECKS["I have The Matrix"] = spin(5, lambda: matrix is not None and matrix.have == ["Alex"])
    window.sidebar.vote.voteRequested.emit(matrix.key)
    spin(0.5)
    panel = window.sidebar.vote
    CHECKS["vote panel shows"] = panel.isVisible() and panel.list.count() == 2 and matrix.voters == ["Alex"]
    CHECKS["my vote marked"] = any(panel.list.item(i).text().startswith("✓ The Matrix") for i in range(2))
    dialog.hide()
    shot(window, "04_vote")
    panel.close_button.click()
    spin(0.5)
    CHECKS["the winner"] = votes.vote.winner == matrix.key and "won" in panel.status.text()
    CHECKS["play it shown"] = panel.play_button.isVisible()
    shot(window, "05_winner")
    panel.play_button.click()
    CHECKS["the winner plays"] = spin(20, lambda: window.player.is_ready() and window.controller.film is not None
                                      and window.controller.film.get("name") == MATRIX)

    # 4. no key of my own (0.5.1): the relay asks TMDB with its key
    relay.tmdb_key, relay.tmdb_base = KEY, server.api
    window.settings.set("discover.tmdb_key", "")
    window.discover.open("mine")
    dialog.search.setText("interstellar")
    dialog._search()
    CHECKS["no own key: search through the relay"] = spin(20, lambda: titles(dialog, "search") == ["Interstellar"])
    CHECKS["no own key: no key note"] = "themoviedb.org" not in dialog.note.text()
    shot(dialog, "06_through_the_relay")

    # 5. the relay has no key either: how to get one, and my films are still there
    relay.tmdb_key = ""
    dialog.search.setText("inception")
    dialog._search()
    CHECKS["no key anywhere: how to get one"] = spin(20, lambda: "themoviedb.org" in dialog.note.text())
    window.discover.open("mine")
    spin(0.5)
    CHECKS["no key anywhere: your films still listed"] = titles(dialog, "mine") == ["Inception", "The Matrix"]
    shot(dialog, "07_no_key")
    window.discover.shutdown()


def scripted_exec(self):
    from syncwatch import errors
    seen = []
    errors.set_handler(lambda report: seen.append(report.code))
    try:
        run_script()
    except Exception:
        import traceback
        traceback.print_exc()
        CHECKS["script ran"] = False
    print("ERRORS SEEN:", seen)
    CHECKS["no problems"] = seen == []
    for widget in QApplication.topLevelWidgets():
        widget.close()
    return 0


QApplication.exec = scripted_exec
app_module.main([])
logs = "".join(path.read_text(encoding="utf-8", errors="replace") for path in data_dir.rglob("*.log"))
CHECKS["the key isn't in the logs"] = KEY not in logs
cache = (data_dir / "profiles" / "default").rglob("tmdb-cache.json")
CHECKS["the key isn't in the cache"] = all(KEY not in path.read_text(encoding="utf-8") for path in cache)
server.stop()
relay.stop()
shutil.rmtree(data_dir, ignore_errors=True)
print("DISCOVER CHECKS:", CHECKS)
failed = [name for name, ok in CHECKS.items() if not ok]
if failed:
    print("DISCOVER HARNESS FAILED:", failed)
    sys.exit(1)
print("DISCOVER HARNESS OK")
sys.exit(0)
