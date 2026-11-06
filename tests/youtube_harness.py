"""Runs the real app (offscreen) through YouTube: a pasted link, the queue, skipping, a broken video.

Usage:  QT_QPA_PLATFORM=offscreen python tests/youtube_harness.py <output dir>

YouTube itself isn't used: the resolver (resolve.py, a real separate process with yt-dlp) is
pointed at a little web server on this PC, one short film per "video ID". Everything else is the
real thing: the link box, finding videos in the background, mpv playing the streams, the queue
starting the next video at the end, Skip, and a video that can't be found being skipped.

1. Paste a link on the start screen: the video opens and plays.
2. Queue two more (one of them "deleted"), then let the video end: the next one starts by itself.
3. Skip: the broken one can't be opened, so the one after it plays. Then a playlist link: its videos
   (read by yt-dlp from a page with them embedded) go into Up next.
4. A torrent (from 0.6, if libtorrent is installed): a .torrent file of a short film, shared by a
   libtorrent session in this process on 127.0.0.1, streams and plays; closing it stops the helper.
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
faulthandler.enable()   # a crash inside Qt or mpv prints where every thread was

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
    {"_version": 1, "values": {"profile.name": "Alex", "ui.startup_checks": "never", "torrents.dht": False,
                               "torrents.incoming": False, "torrents.start_mb": 1,
                               "torrents.folder": str(data_dir / "torrents"),
                               "paths.movies_dir": str(data_dir / "library"),
                               "youtube.playlists": "all"}}), encoding="utf-8")

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

# Playlists (0.5): the playlist is a page with the videos embedded, read by yt-dlp's real code
PLAYLIST = "PLsyncwatchtest1"
(web_dir / f"{PLAYLIST}.html").write_text(
    "<html><head><title>Harness playlist</title></head><body>"
    + "".join(f'<iframe src="https://www.youtube.com/embed/{v}"></iframe>' for v in (FIRST, SECOND, THIRD))
    + "</body></html>", encoding="utf-8")
_playlist_start = youtube_ui.PlaylistJob.start


def _local_playlist(self):
    self._arguments += ["--test-url", f"{BASE}/{self.video_id}.html"]
    _playlist_start(self)


youtube_ui.PlaylistJob.start = _local_playlist


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

    # 3b. a playlist link while a video plays: all its videos go into Up next (setting: all)
    window.open_playlist(youtube_ui.links.PlaylistRef(PLAYLIST), play=False)
    CHECKS["playlist into the queue"] = spin(30, lambda: [i.id for i in window.controller.queue.items]
                                             == [FIRST, SECOND, THIRD])
    CHECKS["said once for the playlist"] = any("Added 3 videos from “Harness playlist”" in
                                               window.sidebar.activity.list.item(i).text()
                                               for i in range(window.sidebar.activity.list.count()))
    torrent_step(window)
    print("activity:", [window.sidebar.activity.list.item(i).text()
                        for i in range(window.sidebar.activity.list.count())][-8:])


def torrent_step(window):
    try:
        import libtorrent as lt
    except ImportError:
        if os.environ.get("GITHUB_ACTIONS"):
            CHECKS["libtorrent installed"] = False
        return
    import warnings
    seed_dir = data_dir / "seed"
    seed_dir.mkdir()
    make_film(seed_dir / "Torrent Film.mkv", seconds=6)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        storage = lt.file_storage()
        lt.add_files(storage, str(seed_dir / "Torrent Film.mkv"))
        maker = lt.create_torrent(storage, 16384)
        lt.set_piece_hashes(maker, str(seed_dir))
    torrent = data_dir / "Torrent Film.torrent"
    torrent.write_bytes(lt.bencode(maker.generate()))
    seeder = lt.session({"listen_interfaces": "127.0.0.1:0", "enable_dht": False, "enable_lsd": False,
                         "enable_upnp": False, "enable_natpmp": False})
    params = lt.add_torrent_params()
    params.ti = lt.torrent_info(str(torrent))
    params.save_path = str(seed_dir)
    handle = seeder.add_torrent(params)
    spin(10, lambda: handle.status().is_seeding)
    window.open_torrent(str(torrent))
    window.torrents.add_peer(f"127.0.0.1:{seeder.listen_port()}")
    playing_torrent = lambda: (getattr(window.player, "stream", None) is not None
                               and window.player.stream.kind == "torrent" and window.player.is_ready())
    CHECKS["torrent streams and opens"] = spin(40, playing_torrent)
    CHECKS["torrent film has its length"] = abs(window.player.duration() - 6000) < 400
    spin(1.5)
    CHECKS["download shown in the control bar"] = window.controls.source_label.text().startswith("⬇")
    window.controller.toggle()
    CHECKS["torrent film plays"] = spin(5, window.player.is_playing)
    shot(window, "05_torrent")
    window.close_film()
    CHECKS["closing stops the torrent"] = not window.torrents.active
    CHECKS["finished film moved into the library"] = (data_dir / "library" / "Torrent Film.mkv").is_file() \
        and not (data_dir / "torrents" / "Torrent Film.mkv").exists()
    print("torrent activity:", [window.sidebar.activity.list.item(i).text()
                                for i in range(window.sidebar.activity.list.count())][-4:])


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
