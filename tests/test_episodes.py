"""Episodes of a show from file names, and which one comes next (library/episodes.py)."""
from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from syncwatch.library.episodes import Episode, find, info, next_after  # noqa: E402


class NameTests(unittest.TestCase):
    def test_names(self):
        cases = {
            "Breaking.Bad.S01E02.720p.BluRay.x264.mkv": Episode("breaking bad", 1, 2),
            "breaking bad s1e3.mp4": Episode("breaking bad", 1, 3),
            "Breaking Bad (2008) - S01E04 - Cancer Man.mkv": Episode("breaking bad", 1, 4),
            "The Office - 2x05 - Halloween.mkv": Episode("the office", 2, 5),
            "Naruto Episode 12.mp4": Episode("naruto", 1, 12),
            "Dark.S01E01.German.1080p.mkv": Episode("dark", 1, 1),
            "S01E07.mkv": Episode("", 1, 7),
        }
        for name, expected in cases.items():
            self.assertEqual(info(name), expected, name)

    def test_films_and_other_things_arent_episodes(self):
        for name in ("Inception.2010.1080p.mkv", "Deep.6.mkv", "Step.Brothers.2008.mkv", "1920x1080 sample.mkv",
                     "The.Matrix.1999.mkv", "Se7en.1995.mkv"):
            self.assertIsNone(info(name), name)

    def test_label(self):
        self.assertEqual(Episode("x", 2, 5).label, "S02E05")


class FolderTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, True)

    def touch(self, *names):
        for name in names:
            path = self.dir / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"x")
        return [self.dir / name for name in names]

    def test_the_next_one_in_the_folder(self):
        e1, e2, e4, other, subs = self.touch("Show.S01E01.mkv", "Show.S01E02.mkv", "Show.S01E04.mkv",
                                             "Other.Show.S01E02.mkv", "Show.S01E03.srt")
        self.assertEqual(next_after(e1), e2)
        self.assertEqual(next_after(e2), e4)           # E03 is missing (only its subtitles are there)
        self.assertIsNone(next_after(e4))
        self.assertIsNone(next_after(other))           # another show's episodes don't count
        film, = self.touch("Inception.2010.mkv")
        self.assertIsNone(next_after(film))

    def test_the_next_season_next_to_it(self):
        last, first_next, later = self.touch("Show/Season 1/Show.S01E10.mkv", "Show/Season 2/Show.S02E01.mkv",
                                             "Show/Season 3/Show.S03E01.mkv")
        self.assertEqual(next_after(last), first_next)

    def test_a_members_copy_is_found_under_another_name(self):
        mine, = self.touch("Mine/Season 1/Show 1x01 (other release).mkv")
        wanted, = self.touch("Mine/Season 2/show.s02e01.web.mkv")
        self.assertEqual(find(Episode("show", 2, 1), mine), wanted)
        self.assertIsNone(find(Episode("show", 2, 2), mine))


if __name__ == "__main__":
    unittest.main()
