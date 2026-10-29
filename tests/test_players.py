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
