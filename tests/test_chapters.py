"""Which chapter is an intro, an opening or a recap (library/chapters.py), and mpv telling the chapters."""
from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from syncwatch.library.chapters import skippable_at  # noqa: E402

SHOW = [(0, "Recap"), (60_000, "Opening"), (150_000, "Episode"), (1_380_000, "Ending"), (1_470_000, "Preview")]


class RuleTests(unittest.TestCase):
    def test_intros_openings_and_recaps(self):
        self.assertEqual(skippable_at(SHOW, 5_000, 1_500_000), (60_000, "Recap"))
        self.assertEqual(skippable_at(SHOW, 61_000, 1_500_000), (150_000, "Opening"))
        self.assertIsNone(skippable_at(SHOW, 200_000, 1_500_000))         # the episode itself
        self.assertIsNone(skippable_at(SHOW, 1_400_000, 1_500_000))       # (endings aren't skipped)
        self.assertIsNone(skippable_at(SHOW, 149_500, 1_500_000))         # its last second: nothing to skip
        self.assertEqual(skippable_at([(0, "OP"), (90_000, "Part A")], 0, 1_000_000), (90_000, "OP"))
        self.assertEqual(skippable_at([(0, "Intro")], 10_000, 60_000), (60_000, "Intro"))   # last chapter: the end

    def test_not_by_mistake(self):
        self.assertIsNone(skippable_at([(0, "Chapter 1"), (90_000, "Chapter 2")], 1_000, 1_000_000))
        self.assertIsNone(skippable_at([(0, "Opening"), (600_000, "Rest")], 1_000, 1_000_000))   # too long
        self.assertIsNone(skippable_at([(0, "Topless"), (90_000, "Chapter 2")], 1_000, 1_000_000))
        self.assertIsNone(skippable_at([], 0, 1_000_000))


try:
    from syncwatch import mpvlib
    mpvlib.load()
    HAVE_MPV = True
except Exception:  # noqa: BLE001 - mpv isn't there: the player test is skipped
    HAVE_MPV = False


@unittest.skipUnless(HAVE_MPV, "libmpv isn't installed")
class MpvChapterTests(unittest.TestCase):
    def test_mpv_tells_the_chapters(self):
        from PySide6.QtCore import QCoreApplication
        from syncwatch.ui.mpv_player import MpvPlayer
        from tests.media_maker import make_film
        app = QCoreApplication.instance() or QCoreApplication([])  # noqa: F841
        folder = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, folder, True)
        film = folder / "Show.S01E01.mkv"
        make_film(film, seconds=5, chapters=[(0, 2, "Intro"), (2, 5, "Episode")])
        player = MpvPlayer(mpvlib.load(), "none")
        self.addCleanup(player.shutdown)
        player.start()
        self.assertTrue(player.load(film))
        import time
        end = time.monotonic() + 10
        while not player.chapters() and time.monotonic() < end:
            QCoreApplication.processEvents()
            player.tick()
            time.sleep(0.02)
        self.assertEqual(player.chapters(), [(0, "Intro"), (2000, "Episode")])


if __name__ == "__main__":
    unittest.main()
