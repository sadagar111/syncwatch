"""Runs the real app (offscreen) through the film library: Add film, conversion, removal.

Usage:  QT_QPA_PLATFORM=offscreen python tests/library_harness.py <output dir>

1. Add film (a normal film): checked, copied into the library, opened.
2. Add the same film again: recognised, not copied twice.
3. Add a film with AC3 sound while the player is told it can't play AC3: the converter
   offers itself, converts the sound to AAC (picture untouched), and the copy opens.
4. Convert the sound of a library film by hand, then remove it (Recycle Bin / deleted).
Screenshots of each step go to the output folder. Exit code 0 only if every check passes.
"""
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
OUT = Path(sys.argv[1])
OUT.mkdir(parents=True, exist_ok=True)
FILM = ROOT / "tests" / "media" / "demo.mkv"

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
