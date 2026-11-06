"""Runs the real app (offscreen) through YouTube: a pasted link, the queue, skipping, a broken video.

Usage:  QT_QPA_PLATFORM=offscreen python tests/youtube_harness.py <output dir>

YouTube itself isn't used: the resolver (resolve.py, a real separate process with yt-dlp) is
pointed at a little web server on this PC, one short film per "video ID". Everything else is the
real thing: the link box, finding videos in the background, mpv playing the streams, the queue
starting the next video at the end, Skip, and a video that can't be found being skipped.

1. Paste a link on the start screen: the video opens and plays.
2. Queue two more (one of them "deleted"), then let the video end: the next one starts by itself.
3. Skip: the broken one can't be opened, so the one after it plays.
Screenshots go to the output folder. Exit code 0 only if every check passes.
"""
import functools
import http.server
import json
import os
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path
import faulthandler
faulthandler.enable(all_threads=sys.platform != "win32")   # a crash inside Qt or mpv prints where every thread was

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
OUT = Path(sys.argv[1])
OUT.mkdir(parents=True, exist_ok=True)

data_dir = Path(tempfile.mkdtemp(prefix="sw-yt-"))
os.environ["SYNCWATCH_DATA_DIR"] = str(data_dir)
os.environ["no_proxy"] = os.environ["NO_PROXY"] = "127.0.0.1,localhost"
profile_dir = data_dir / "profiles" / "default"
profile_dir.mkdir(parents=True)
(profile_dir / "settings.json").write_text(json.dumps(
    {"_version": 1, "values": {"profile.name": "Alex", "ui.startup_checks": "never"}}), encoding="utf-8")

from PySide6.QtWidgets import QApplication  # noqa: E402

from syncwatch import app as app_module  # noqa: E402
from syncwatch.ui import youtube_ui  # noqa: E402
from tests.media_maker import make_film  # noqa: E402

FIRST, SECOND, GONE, THIRD = "firstvideo1", "secondvideo", "deletedvid1", "thirdvideo1"
CHECKS = {}
web_dir = data_dir / "web"
web_dir.mkdir()
for video, seconds in ((FIRST, 4), (SECOND, 4), (THIRD, 4)):
    make_film(web_dir / f"{video}.mkv", seconds=seconds)


class Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(Quiet, directory=str(web_dir)))
threading.Thread(target=server.serve_forever, daemon=True).start()
BASE = f"http://127.0.0.1:{server.server_address[1]}"

# The only change to the app: every video ID is looked up on the local web server instead of YouTube
_new_job = youtube_ui.YouTube._new_job


def _local_job(self, video_id):
    job = _new_job(self, video_id)
    job._arguments += ["--test-url", f"{BASE}/{video_id}.mkv"]
    return job


youtube_ui.YouTube._new_job = _local_job
youtube_ui.find_js_runtime = lambda setting: setting or "/no/deno/needed/for/local/files"


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


def playing(window, video_id):
    stream = getattr(window.player, "stream", None)
    return stream is not None and stream.id == video_id and window.player.is_playing()


def run_script():
    window = main_window()
    window.resize(1280, 760)
    spin(1.0)
    shot(window, "01_start")

    # 1. a pasted link
    window.idle.youtube_box.field.setText(f"https://youtu.be/{FIRST}")
    window.idle.youtube_box.button.click()
    CHECKS["link opens the video"] = spin(30, lambda: getattr(window.player, "stream", None) is not None
                                          and window.player.is_ready())
    CHECKS["it plays like a film"] = abs(window.player.duration() - 4000) < 300
    window.controller.toggle()
    CHECKS["play"] = spin(5, lambda: playing(window, FIRST))
    shot(window, "02_first_video")

    # 2. queue two more, one of them gone from "YouTube"; at the end the next one starts by itself
    for video in (SECOND, GONE, THIRD):
        window.queue_youtube(youtube_ui.links.VideoRef(video))
    spin(0.5)
    CHECKS["queue holds three"] = [item.id for item in window.controller.queue.items] == [SECOND, GONE, THIRD]
    CHECKS["queue panel shows"] = window.sidebar.queue.isVisible() and window.sidebar.queue.list.count() == 3
    shot(window, "03_queue")
    window.controller.seek(3000)
    CHECKS["next video starts at the end"] = spin(30, lambda: playing(window, SECOND))
    CHECKS["queue moved on"] = [item.id for item in window.controller.queue.items] == [GONE, THIRD]

    # 3. skip: the "deleted" video can't be found (404), so the one after it plays
    window.controller.queue_skip()
    CHECKS["broken video skipped, the next plays"] = spin(40, lambda: playing(window, THIRD))
    CHECKS["queue empty"] = window.controller.queue.items == []
    shot(window, "04_after_skip")
    print("activity:", [window.sidebar.activity.list.item(i).text()
                        for i in range(window.sidebar.activity.list.count())][-8:])


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
    CHECKS["only the deleted video was a problem"] = set(seen) <= {"SW-602"}
    for widget in QApplication.topLevelWidgets():
        widget.close()
    return 0


QApplication.exec = scripted_exec
app_module.main([])
server.shutdown()
shutil.rmtree(data_dir, ignore_errors=True)
print("YOUTUBE CHECKS:", CHECKS)
failed = [name for name, ok in CHECKS.items() if not ok]
if failed:
    print("YOUTUBE HARNESS FAILED:", failed)
    sys.exit(1)
print("YOUTUBE HARNESS OK")
sys.exit(0)
