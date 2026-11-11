"""Runs the real app (offscreen) through the film library: Add film, conversion, removal.

Usage:  QT_QPA_PLATFORM=offscreen python tests/library_harness.py <output dir>

1. Add film (a normal film): checked, copied into the library, opened.
2. Add the same film again: recognised, not copied twice.
3. Add a film with AC3 sound while the player is told it can't play AC3: the converter
   offers itself, converts the sound to AAC (picture untouched), and the copy opens.
4. Convert the sound of a library film by hand, then remove it (Recycle Bin / deleted).
5-6. A show: the next episode opens at the end; Skip intro.
7. A web link (0.5.1): asked once how links open ("Download it while it plays"): it plays from the
   link while it's saved, then it's in the library; a link to a web page says it's no film.
Screenshots of each step go to the output folder. Exit code 0 only if every check passes.
"""
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path
import faulthandler
faulthandler.enable(all_threads=sys.platform != "win32")   # a crash inside Qt or mpv prints where every thread was

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
OUT = Path(sys.argv[1])
OUT.mkdir(parents=True, exist_ok=True)
FILM = ROOT / "tests" / "media" / "demo.mkv"
os.environ["no_proxy"] = os.environ["NO_PROXY"] = "127.0.0.1,localhost"     # (step 7's web server is local)
ERRORS_SEEN = []

data_dir = Path(tempfile.mkdtemp(prefix="sw-lib-"))
library_dir = data_dir / "library"
downloads = data_dir / "Downloads"
downloads.mkdir(parents=True)
os.environ["SYNCWATCH_DATA_DIR"] = str(data_dir)
profile_dir = data_dir / "profiles" / "default"
profile_dir.mkdir(parents=True)
(profile_dir / "settings.json").write_text(json.dumps(
    {"_version": 1, "values": {"profile.name": "Alex", "ui.startup_checks": "never", "paths.movies_dir": str(library_dir)}}),
    encoding="utf-8")

from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from syncwatch import app as app_module  # noqa: E402
from tests.media_maker import make_film  # noqa: E402

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


def answer_boxes(button_text, seen):
    """Click ``button_text`` in the next question box (they're modal: answered from a timer)."""
    from PySide6.QtCore import QTimer

    def look():
        for widget in QApplication.topLevelWidgets():
            if isinstance(widget, QMessageBox) and widget.isVisible():
                seen.append(widget.text())
                shot(widget, f"question_{len(seen)}")
                for button in widget.buttons():
                    if button.text() == button_text:
                        button.click()
                        return
        QTimer.singleShot(100, look)
    QTimer.singleShot(100, look)


def job_dialogs():
    from syncwatch.ui.library_ui import JobDialog
    return [w for w in QApplication.topLevelWidgets() if isinstance(w, JobDialog) and w.isVisible()]


def run_script():
    window = main_window()
    window.resize(1280, 760)
    spin(1.0)
    shot(window, "01_empty_library")
    lib = window.library

    # 1. a normal film
    source = downloads / "Demo Film.mkv"
    shutil.copyfile(FILM, source)
    seen_steps = []
    lib.start_job(source)
    spin(30, lambda: (seen_steps.append(job_dialogs()[0].step.text()) if job_dialogs() else None) or lib.job is None)
    copied = library_dir / "Demo Film.mkv"
    CHECKS["film copied into the library"] = copied.is_file() and copied.stat().st_size == source.stat().st_size
    CHECKS["original stays"] = source.is_file()
    spin(3, lambda: window.player.is_ready())
    CHECKS["film opened after adding"] = window.player.path == copied and window.player.is_ready()
    print("steps seen:", sorted(set(s for s in seen_steps if s)))
    shot(window, "02_added_and_playing")

    # 2. the same film again: no second copy
    lib.start_job(source)
    spin(10, lambda: lib.job is None)
    CHECKS["same film not copied twice"] = len(list(library_dir.glob("*.mkv"))) == 1

    # 3. a film whose sound "can't be played": the player is told AC3 doesn't work
    ac3_film = downloads / "Film with AC3.mkv"
    make_film(ac3_film, seconds=4, audio_codec="ac3", channels=6)
    import syncwatch.ui.players as players_module
    original_finish = players_module.PlayabilityTest._finish

    def ac3_fails(self, picture, sound, detail):
        if self.path.name == "Film with AC3.mkv":
            sound, detail = False, "simulated: AC3 can't be decoded here"
        return original_finish(self, picture, sound, detail)

    players_module.PlayabilityTest._finish = ac3_fails
    questions = []
    answer_boxes("Convert", questions)
    lib.start_job(ac3_film)
    spin(60, lambda: lib.job is None)
    converted = library_dir / "Film with AC3.mkv"
    CHECKS["conversion offered"] = any("can't play its sound" in q for q in questions)
    CHECKS["converted copy in the library"] = converted.is_file()
    if converted.is_file():
        import av
        with av.open(str(converted)) as container:
            codecs = [(s.type, s.codec_context.name) for s in container.streams]
        print("converted copy:", codecs)
        CHECKS["sound converted to AAC"] = ("audio", "aac") in codecs and ("video", "h264") in codecs
    CHECKS["no .part files left"] = not list(library_dir.glob("*.part"))
    spin(3, lambda: window.player.is_ready())
    shot(window, "03_converted_playing")

    # 3b. a file the player "can't play at all" (only the one in Downloads): a fresh copy is
    # offered, made, tested again (it plays) and kept
    odd = downloads / "Odd Packaging.mkv"
    make_film(odd, seconds=3, audio_codec="aac", channels=2)

    def odd_fails(self, picture, sound, detail):
        if self.path == odd:
            picture, sound, detail = False, None, "simulated: the player can't open it"
        return original_finish(self, picture, sound, detail)

    players_module.PlayabilityTest._finish = odd_fails
    questions.clear()
    answer_boxes("Convert", questions)
    lib.start_job(odd)
    spin(60, lambda: lib.job is None)
    CHECKS["fresh copy offered"] = any("can't play Odd Packaging.mkv as it is" in q for q in questions)
    CHECKS["fresh copy kept because it plays"] = (library_dir / "Odd Packaging.mkv").is_file()
    players_module.PlayabilityTest._finish = original_finish

    # 4. Convert the sound by hand (AC3 film added directly), then remove it
    manual = library_dir / "Manual AC3.mkv"
    make_film(manual, seconds=3, audio_codec="ac3", channels=2)
    lib.refresh()
    questions.clear()
    answer_boxes("Convert", questions)
    lib.convert_film(manual)
    spin(60, lambda: lib.job is None)
    CHECKS["manual conversion made a (converted) copy"] = (library_dir / "Manual AC3 (converted).mkv").is_file()
    lib.open_dialog()
    spin(0.5)
    shot(lib.dialog, "04_library_window")
    answer_boxes("Remove", questions)
    lib.remove_film(manual)
    spin(2, lambda: not manual.exists())
    CHECKS["removed from the library"] = not manual.exists()
    CHECKS["library list shows the films"] = lib.dialog.panel.tree.topLevelItemCount() == len(list(library_dir.glob("*.mkv")))
    print("library now:", sorted(p.name for p in library_dir.iterdir()))
    lib.dialog.close()

    # 5. A show (0.5 next episode): the end of S01E02 opens the next episode, which is in the next
    # season's folder, and it plays by itself
    show = data_dir / "Shows" / "Test Show"
    second, third = show / "Season 1" / "Test.Show.S01E02.mkv", show / "Season 2" / "Test.Show.S02E01.mkv"
    for episode in (second, show / "Season 1" / "Test.Show.S01E01.mkv", third):
        episode.parent.mkdir(parents=True, exist_ok=True)
        make_film(episode, seconds=3)
    window.open_film(second)
    spin(15, lambda: window.player.path == second and window.player.is_ready())
    window.controller.toggle()
    CHECKS["the next episode opens at the end"] = spin(25, lambda: window.player.path == third)
    CHECKS["and plays by itself"] = spin(15, lambda: window.player.path == third and window.player.is_playing())

    # 6. A show's intro (0.5 skip intro): Skip intro shows during the "Intro" chapter, and skips it
    intro_film = data_dir / "Shows" / "Intro.Show.S01E01.mkv"
    intro_film.parent.mkdir(parents=True, exist_ok=True)
    make_film(intro_film, seconds=7, chapters=[(0, 4, "Intro"), (4, 7, "Episode")])
    window.open_film(intro_film)
    spin(15, lambda: window.player.path == intro_film and window.player.is_ready())
    window.controller.toggle()
    CHECKS["Skip intro shows during the intro"] = spin(5, lambda: window.controls.skip_intro.isVisible())
    window.controls.skip_intro.click()
    CHECKS["it jumps past the intro"] = spin(5, lambda: window.player.position() >= 3900)
    CHECKS["and goes away"] = spin(3, lambda: not window.controls.skip_intro.isVisible())

    # 7. A web link (0.5.1): the first time SyncWatch asks how links open; "Download it while it plays":
    # it plays straight from the link while it's saved, then it's in the library
    web_dir = data_dir / "web"
    web_dir.mkdir()
    web_film = web_dir / "Web.Film.2024.mkv"
    make_film(web_film, seconds=8)
    sys.path.insert(0, str(ROOT / "tests"))
    import film_server
    server = film_server.start(web_dir)
    window.close_film()
    box = window.idle.youtube_box
    box.field.setText(f"{server}/d/AbC123/Web.Film.2024.mkv")
    CHECKS["web link: Watch can be clicked"] = box.button.isEnabled() and not box.note.isVisible()
    questions = []
    answer_boxes("Download it while it plays", questions)
    box.button.click()
    CHECKS["web link: asked how links open"] = any("How should SyncWatch open links" in q for q in questions)
    CHECKS["web link: the choice is kept"] = window.settings.get("links.open") == "download"
    CHECKS["web link: plays from the link"] = spin(25, lambda: getattr(window.player.stream, "kind", "") == "web"
                                                    and window.player.is_ready())
    CHECKS["web link: not taken for a YouTube video"] = spin(5, lambda: (window.controller.film or {}).get("name")
                                                              == "Web.Film.2024.mkv"
                                                              and "yt" not in window.controller.film
                                                              and window.controller.queue.now is None)
    shot(window, "07_web_link")
    saved = library_dir / "Web.Film.2024.mkv"
    CHECKS["web link: saved into the library"] = spin(30, lambda: saved.is_file())
    CHECKS["web link: saved completely"] = saved.is_file() and saved.read_bytes() == web_film.read_bytes()
    CHECKS["web link: nothing half-done left"] = not list((library_dir / "Torrents").glob("*.sw-download*"))
    feed = window.sidebar.activity.list
    CHECKS["web link: said so"] = any("in your library now" in feed.item(i).text() for i in range(feed.count()))
    # a link to a web page: no question this time (kept), and it says it's no film
    questions.clear()
    box.field.setText(f"{server}/films/about.html")
    box.button.click()
    CHECKS["web page: no film (SW-643)"] = spin(10, lambda: "SW-643" in ERRORS_SEEN)
    CHECKS["web page: not asked again"] = not questions


def scripted_exec(self):
    from syncwatch import errors
    seen = ERRORS_SEEN
    errors.set_handler(lambda report: seen.append(report.code))
    try:
        run_script()
    except Exception:
        import traceback
        traceback.print_exc()
        CHECKS["script ran"] = False
    print("ERRORS SEEN:", seen)
    for widget in QApplication.topLevelWidgets():
        widget.close()
    return 0


QApplication.exec = scripted_exec
app_module.main([])
print("LIBRARY CHECKS:", CHECKS)
failed = [name for name, ok in CHECKS.items() if not ok]
if failed:
    print("LIBRARY HARNESS FAILED:", failed)
    sys.exit(1)
print("LIBRARY HARNESS OK")
sys.exit(0)
