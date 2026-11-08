"""Tests for the video players: choosing one, mpv's behaviour, the playability test, libmpv loading.

They run the real mpv (without a picture: the test machines have no screen) on the demo film.
Skipped if libmpv isn't installed (Linux: apt install libmpv2; Windows: tools/get_libmpv.py).
"""
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
if sys.platform.startswith("linux"):
    os.environ.setdefault("QT_AUDIO_BACKEND", "pulseaudio")   # avoids a PipeWire crash without a sound server
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from PySide6.QtWidgets import QApplication  # noqa: E402

from syncwatch import errors, mpvlib  # noqa: E402
from syncwatch.settings import Settings  # noqa: E402

APP = QApplication.instance() or QApplication([])
FILM = ROOT / "tests" / "media" / "demo.mkv"
try:
    mpvlib.load()
    HAVE_MPV = True
except mpvlib.Unavailable:
    HAVE_MPV = False
ON_GITHUB = bool(os.environ.get("GITHUB_ACTIONS"))   # there mpv must be installed: never skip silently


def spin(seconds, until=None):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        APP.processEvents()
        if until is not None and until():
            return True
        time.sleep(0.005)
    return bool(until()) if until else None


class Reports:
    """Collects the codes of all problems reported (errors.report) during a test, popups or not."""

    def __enter__(self):
        self.codes = []
        original = errors.report

        def record(code, *args, **kwargs):
            self.codes.append(code)
            return original(code, *args, **kwargs)

        self._patch = mock.patch.object(errors, "report", side_effect=record)
        self._patch.start()
        return self

    def __exit__(self, *exc):
        self._patch.stop()


class InstalledTests(unittest.TestCase):
    @unittest.skipUnless(ON_GITHUB, "only on GitHub's machines")
    def test_mpv_is_installed_on_github(self):
        self.assertTrue(HAVE_MPV, "libmpv should be installed there (apt libmpv2 / tools/get_libmpv.py)")


class ChoosingTests(unittest.TestCase):
    def test_video_output(self):
        from syncwatch.ui.mpv_player import choose_video_output
        self.assertEqual(choose_video_output("window", "offscreen"), "none")
        self.assertEqual(choose_video_output("opengl", "windows"), "opengl")
        self.assertEqual(choose_video_output("window", "xcb"), "window")
        self.assertEqual(choose_video_output("auto", "windows"), "window" if sys.platform == "win32" else "opengl")

    def test_languages(self):
        from syncwatch.ui.mpv_player import language
        self.assertEqual(language("slo"), ("sk", "Slovak"))
        self.assertEqual(language("ces"), ("cs", "Czech"))
        self.assertEqual(language("eng"), ("en", "English"))
        self.assertEqual(language(""), ("", ""))
        self.assertEqual(language("und"), ("", ""))

    def test_qt_when_chosen(self):
        from syncwatch.ui import players
        settings = Settings(None)
        settings.set("playback.engine", "qt")
        player = players.create(settings)
        self.assertEqual(player.engine, "qt")
        self.assertIsNone(players.fallback_reason)

    def test_falls_back_to_qt_when_mpv_cant_load(self):
        from syncwatch.ui import players
        with Reports() as reports, mock.patch.object(mpvlib, "load", side_effect=mpvlib.Unavailable("no DLL here")):
            player = players.create(Settings(None))
        self.assertEqual(player.engine, "qt")
        self.assertEqual(players.fallback_reason, "no DLL here")
        self.assertEqual(reports.codes, ["SW-209"])

    def test_falls_back_when_mpv_cant_start(self):
        from syncwatch.ui import players
        from syncwatch.ui.mpv_player import MpvPlayer, StartFailed
        placed = []
        broken = MpvPlayer(object(), "none")
        with Reports() as reports, mock.patch.object(MpvPlayer, "start", side_effect=StartFailed("boom")):
            player = players.start_or_fallback(broken, placed.append)
        self.assertEqual(player.engine, "qt")
        self.assertEqual(placed, [player])
        self.assertEqual(reports.codes, ["SW-209"])


class WinfixTests(unittest.TestCase):
    def test_nothing_happens_elsewhere(self):
        from syncwatch.ui import winfix
        if sys.platform != "win32":
            self.assertEqual(winfix.disable_child_windows(12345), 0)
            winfix.set_border(12345, True)   # no error

    @unittest.skipUnless(sys.platform == "win32", "Windows only")
    def test_on_real_windows(self):
        """With plain Win32 windows (the test machines have no screen for Qt's own)."""
        import ctypes
        from ctypes import wintypes
        from syncwatch.ui import winfix
        user32 = ctypes.WinDLL("user32")
        user32.CreateWindowExW.restype = wintypes.HWND
        user32.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
                                           ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.HWND,
                                           wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID]
        user32.IsWindowEnabled.argtypes = [wintypes.HWND]
        user32.DestroyWindow.argtypes = [wintypes.HWND]
        user32.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
        user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
        parent = user32.CreateWindowExW(0, "STATIC", "parent", 0, 0, 0, 200, 200, None, None, None, None)
        child = user32.CreateWindowExW(0, "STATIC", "child", 0x40000000, 0, 0, 50, 50, parent, None, None, None)
        self.assertTrue(parent and child)
        try:
            self.assertEqual(winfix.disable_child_windows(parent), 1)
            self.assertFalse(user32.IsWindowEnabled(child))
            self.assertTrue(user32.IsWindowEnabled(parent))
            self.assertEqual(winfix.disable_child_windows(parent), 0)   # already done
            winfix.set_border(parent, True)
            self.assertTrue(user32.GetWindowLongPtrW(parent, winfix.GWL_STYLE) & winfix.WS_BORDER)
            winfix.set_border(parent, False)
            self.assertFalse(user32.GetWindowLongPtrW(parent, winfix.GWL_STYLE) & winfix.WS_BORDER)
        finally:
            user32.DestroyWindow(child)
            user32.DestroyWindow(parent)


class LibmpvSearchTests(unittest.TestCase):
    def test_own_folders_only_and_override_first(self):
        folder = Path(tempfile.mkdtemp())
        try:
            (folder / mpvlib.DLL_NAME).write_bytes(b"not really a DLL")
            with mock.patch.dict(os.environ, {mpvlib.ENV_DIR: str(folder)}):
                self.assertEqual(mpvlib.candidate_dirs()[0], folder)
                self.assertEqual(mpvlib.find_dll(), folder / mpvlib.DLL_NAME)
            self.assertNotIn(folder, mpvlib.candidate_dirs())
        finally:
            shutil.rmtree(folder, ignore_errors=True)


@unittest.skipUnless(HAVE_MPV, "libmpv isn't installed")
class MpvPlayerTests(unittest.TestCase):
    def setUp(self):
        from syncwatch.ui.mpv_player import MpvPlayer
        self.player = MpvPlayer(mpvlib.load(), "none")
        self.player.video_widget("#000000")
        self.player.start()
        self.events = []
        self.player.loaded.connect(lambda: self.events.append("loaded"))
        self.player.playingChanged.connect(lambda playing: self.events.append(("playing", playing)))
        self.player.ended.connect(lambda: self.events.append("ended"))

    def tearDown(self):
        self.player.shutdown()
        self.player.shutdown()   # twice is fine

    def load(self):
        self.assertTrue(self.player.load(FILM))
        self.assertTrue(spin(10, self.player.is_ready), "the film never became ready")

    def test_load(self):
        self.assertFalse(self.player.has_media())
        self.load()
        self.assertIn("loaded", self.events)
        self.assertAlmostEqual(self.player.duration(), 40_023, delta=100)
        self.assertFalse(self.player.is_playing())       # opens paused, showing the first picture
        self.assertEqual(self.player.position(), 0.0)
        self.assertEqual([label for _, label, _ in self.player.audio_tracks()], ["Track 1 · AAC mono"])
        self.assertEqual(self.player.active_audio_track(), 0)

    def test_night_mode(self):
        self.load()
        self.player.set_night_mode(True)
        self.assertTrue(spin(5, lambda: "dynaudnorm" in str(self.player._read("af"))))
        self.player.play()                                  # it still plays, with the filter
        self.assertTrue(spin(5, lambda: self.player.position() > 300))
        self.player.set_night_mode(False)
        self.assertTrue(spin(5, lambda: not self.player._read("af")))

    def test_play_pause_and_position(self):
        self.load()
        self.player.play()
        self.assertTrue(self.player.is_playing())
        started = time.monotonic()
        spin(1.0)
        elapsed = (time.monotonic() - started) * 1000
        self.assertAlmostEqual(self.player.position(), elapsed, delta=250)
        self.player.pause()
        frozen = self.player.position()
        spin(0.3)
        self.assertAlmostEqual(self.player.position(), frozen, delta=60)
        self.assertEqual([e for e in self.events if isinstance(e, tuple)], [("playing", True), ("playing", False)])

    def test_jump_holds_the_target_until_ready(self):
        self.load()
        self.player.seek(20_000)
        self.assertFalse(self.player.is_ready())
        self.assertEqual(self.player.position(), 20_000)
        self.assertTrue(spin(5, self.player.is_ready))
        self.assertAlmostEqual(self.player.position(), 20_000, delta=50)
        self.player.play()
        self.player.seek(30_000)
        self.assertEqual(self.player.position(), 30_000)
        self.assertTrue(spin(5, self.player.is_ready))
        ready_at = time.monotonic()
        spin(0.5)
        self.assertAlmostEqual(self.player.position(), 30_000 + (time.monotonic() - ready_at) * 1000, delta=200)

    def test_quick_jumps_end_at_the_last_one(self):
        self.load()
        self.player.play()
        for target in (5_000, 25_000, 12_000):
            self.player.seek(target)
        self.assertEqual(self.player.position(), 12_000)
        self.assertTrue(spin(5, self.player.is_ready))
        self.assertAlmostEqual(self.player.position(), 12_000, delta=150)

    def test_jump_right_after_loading(self):
        self.assertTrue(self.player.load(FILM))
        self.assertTrue(spin(5, self.player.has_media))
        self.player.seek(15_000)          # like "continue where I stopped"
        self.assertTrue(spin(5, self.player.is_ready))
        self.assertAlmostEqual(self.player.position(), 15_000, delta=50)

    def test_end_of_film(self):
        self.load()
        self.player.seek(39_500)
        spin(3, self.player.is_ready)
        self.player.play()
        self.assertTrue(spin(5, lambda: "ended" in self.events))
        self.assertFalse(self.player.is_playing())

    def test_volume(self):
        self.player.set_volume(35, True)
        spin(0.3)
        self.assertEqual(self.player.core.volume, 35)
        self.assertTrue(self.player.core.mute)

    def test_problems(self):
        with Reports() as reports:
            self.assertFalse(self.player.load(Path(tempfile.gettempdir()) / "surely-not-here.mkv"))
            junk = Path(tempfile.mkdtemp()) / "junk.mkv"
            junk.write_bytes(b"definitely not a film" * 1000)
            try:
                self.assertTrue(self.player.load(junk))
                self.assertTrue(spin(5, lambda: "SW-205" in reports.codes), reports.codes)
            finally:
                shutil.rmtree(junk.parent, ignore_errors=True)
        self.assertEqual(reports.codes[0], "SW-204")
        self.assertFalse(self.player.has_media())

    def test_a_replaced_film_doesnt_confuse_it(self):
        self.player.load(FILM)
        other = Path(tempfile.mkdtemp()) / "copy.mkv"
        shutil.copyfile(FILM, other)
        try:
            self.player.load(other)      # straight away, before the first one finished loading
            self.assertTrue(spin(10, self.player.is_ready))
            self.assertEqual(self.player.path, other)
            self.assertEqual(self.player.position(), 0.0)
        finally:
            shutil.rmtree(other.parent, ignore_errors=True)


@unittest.skipUnless(HAVE_MPV, "libmpv isn't installed")
class _HeaderRecorder:
    """A local web server that remembers the headers of every request (a stand-in for YouTube)."""

    def __init__(self, folder: Path):
        import functools
        import http.server
        import threading
        seen = self.headers = []

        class Handler(http.server.SimpleHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def end_headers(self):
                seen.append(dict(self.headers))
                super().end_headers()

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(Handler, directory=str(folder)))
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self):
        self.server.shutdown()


@unittest.skipUnless(HAVE_MPV or ON_GITHUB, "libmpv isn't installed")
class StreamTests(unittest.TestCase):
    """A YouTube-like video: picture and sound as two separate web addresses, joined by an EDL."""

    @classmethod
    def setUpClass(cls):
        try:
            from tests.media_maker import make_film, make_sound
        except ImportError as exc:   # PyAV missing (it's required on GitHub)
            raise unittest.SkipTest(f"PyAV isn't installed: {exc}")
        cls.dir = Path(tempfile.mkdtemp())
        make_film(cls.dir / "picture.mkv", seconds=4, audio_codec=None)
        make_sound(cls.dir / "sound.mka", seconds=4)
        cls.web = _HeaderRecorder(cls.dir)

    @classmethod
    def tearDownClass(cls):
        cls.web.close()
        shutil.rmtree(cls.dir, ignore_errors=True)

    def setUp(self):
        from syncwatch.ui.mpv_player import MpvPlayer
        self.player = MpvPlayer(mpvlib.load(), "none")
        self.player.video_widget("#000000")
        self.player.start()

    def tearDown(self):
        self.player.shutdown()

    def stream(self, **changes):
        from syncwatch.youtube.streams import Stream
        values = dict(id="dQw4w9WgXcQ", title="Test", channel="", duration_s=4.0, thumbnail="",
                      video_url=f"{self.web.base}/picture.mkv", audio_url=f"{self.web.base}/sound.mka",
                      headers={"User-Agent": "SyncWatchTest/1", "Accept-Language": "en-us,en;q=0.5"},
                      expires=time.time() + 3600)
        values.update(changes)
        return Stream(**values)

    def test_picture_and_sound_play_as_one(self):
        self.web.headers.clear()
        self.assertTrue(self.player.load_stream(self.stream()))
        self.assertTrue(spin(15, self.player.is_ready), "the stream never became ready")
        self.assertTrue(self.player.has_media())
        self.assertIsNone(self.player.path)
        self.assertAlmostEqual(self.player.duration(), 4000, delta=150)
        self.assertEqual(len(self.player.audio_tracks()), 1)      # the sound stream joined the picture
        self.player.seek(2500)
        self.assertTrue(spin(10, self.player.is_ready))
        self.assertAlmostEqual(self.player.position(), 2500, delta=100)
        agents = {h.get("User-Agent") for h in self.web.headers}
        self.assertEqual(agents, {"SyncWatchTest/1"})
        self.assertTrue(all(h.get("Accept-Language") == "en-us,en;q=0.5" for h in self.web.headers))

    def test_a_file_after_a_stream_gets_no_stream_headers(self):
        self.assertTrue(self.player.load_stream(self.stream(audio_url="")))
        self.assertTrue(spin(15, self.player.is_ready))
        self.assertTrue(self.player.load(FILM))
        self.assertTrue(spin(10, self.player.is_ready))
        self.assertIsNone(self.player.stream)
        self.assertEqual(self.player.path, FILM)

    def test_a_broken_stream_is_reported(self):
        with Reports() as reports:
            self.assertTrue(self.player.load_stream(self.stream(video_url=f"{self.web.base}/missing.mkv",
                                                                audio_url="")))
            self.assertTrue(spin(15, lambda: "SW-609" in reports.codes), reports.codes)
        self.assertFalse(self.player.has_media())


class PlayabilityTests(unittest.TestCase):
    def run_test(self, engine, path):
        from syncwatch.ui.players import PlayabilityTest
        results = []
        test = PlayabilityTest(engine, path)
        test.finished.connect(results.append)
        test.start()
        self.assertTrue(spin(15, lambda: results), "the test never finished")
        return results[0]

    def test_demo_film_plays_with_both_players(self):
        for engine in ("mpv", "qt"):
            with self.subTest(engine):
                result = self.run_test(engine, FILM)
                self.assertTrue(result["picture"], result)
                self.assertTrue(result["sound"], result)

    def test_junk_doesnt_play(self):
        junk = Path(tempfile.mkdtemp()) / "junk.mkv"
        junk.write_bytes(b"definitely not a film" * 1000)
        try:
            self.assertFalse(self.run_test("mpv", junk)["picture"])
        finally:
            shutil.rmtree(junk.parent, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
