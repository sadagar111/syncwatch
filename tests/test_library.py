"""Tests for the film library (copying, duplicates, names, space) and the built-in converter."""
import collections
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from syncwatch.library import convert, films  # noqa: E402
from syncwatch.units import Eta, human_size, human_time  # noqa: E402

try:
    import av
    from tests.media_maker import make_film
    HAVE_AV = True
except ImportError:  # the converter tests need PyAV
    HAVE_AV = False

CONVERTER = ROOT / "syncwatch" / "library" / "convert.py"


class InstalledTests(unittest.TestCase):
    @unittest.skipUnless(os.environ.get("GITHUB_ACTIONS"), "only on GitHub's machines")
    def test_pyav_is_installed_on_github(self):
        self.assertTrue(HAVE_AV, "PyAV should be installed there (requirements.txt)")


class LibraryFolderTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.library = self.dir / "library"
        self.library.mkdir()

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def write(self, path: Path, data: bytes) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def test_list_only_films_sorted(self):
        for name in ("b film.MKV", "A film.mp4", "notes.txt", "half.mkv.part", ".hidden.mkv"):
            self.write(self.library / name, b"x")
        (self.library / "folder.mkv").mkdir()
        self.assertEqual([f.name for f in films.list_films(self.library)], ["A film.mp4", "b film.MKV"])
        self.assertEqual(films.list_films(self.dir / "missing"), [])

    def test_unique_names(self):
        self.assertEqual(films.unique_path(self.library, "Film.mkv").name, "Film.mkv")
        self.write(self.library / "Film.mkv", b"1")
        self.write(self.library / "Film (2).mkv.part", b"2")   # a copy still running counts as taken
        self.assertEqual(films.unique_path(self.library, "Film.mkv").name, "Film (3).mkv")

    def test_inside(self):
        film = self.write(self.library / "a.mkv", b"x")
        self.assertTrue(films.is_inside(self.library, film))
        self.assertFalse(films.is_inside(self.library, self.dir / "a.mkv"))

    def test_finds_same_content_not_just_same_size(self):
        big = os.urandom(3 * films.FINGERPRINT_BLOCK)
        source = self.write(self.dir / "Downloads" / "x.mkv", big)
        self.write(self.library / "same size other content.mkv", os.urandom(len(big)))
        self.assertIsNone(films.find_copy(self.library, source))
        copy = self.write(self.library / "renamed copy.mkv", big)
        self.assertEqual(films.find_copy(self.library, source), copy)

    def test_copy_with_progress(self):
        data = os.urandom(films.COPY_BLOCK * 2 + 123)
        source = self.write(self.dir / "src.mkv", data)
        os.utime(source, (1_600_000_000, 1_600_000_000))
        reports = []
        target = films.copy_film(source, self.library / "src.mkv", lambda done, total: reports.append((done, total)))
        self.assertEqual(target.read_bytes(), data)
        self.assertEqual(reports[0], (0, len(data)))
        self.assertEqual(reports[-1], (len(data), len(data)))
        self.assertEqual(int(target.stat().st_mtime), 1_600_000_000)
        self.assertFalse(list(self.library.glob("*.part")))
        self.assertTrue(source.exists())

    def test_cancelled_copy_leaves_nothing(self):
        source = self.write(self.dir / "src.mkv", os.urandom(films.COPY_BLOCK * 3))
        calls = []

        def cancelled():
            calls.append(1)
            return len(calls) > 2

        with self.assertRaises(films.Cancelled):
            films.copy_film(source, self.library / "src.mkv", cancelled=cancelled)
        self.assertEqual(list(self.library.iterdir()), [])

    def test_stale_part_files_are_cleaned_up(self):
        old = self.write(self.library / "Film.mkv.part", b"half")
        fresh = self.write(self.library / "Other.mp4.part", b"running now")
        notes = self.write(self.library / "notes.part", b"not ours")
        os.utime(old, (1_600_000_000, 1_600_000_000))
        os.utime(notes, (1_600_000_000, 1_600_000_000))
        self.assertEqual(films.remove_stale_parts(self.library), [old])
        self.assertTrue(fresh.exists() and notes.exists())

    def test_space(self):
        usage = collections.namedtuple("usage", "total used free")(1000, 0, films.SPACE_MARGIN + 500)
        with mock.patch.object(films.shutil, "disk_usage", return_value=usage):
            self.assertIsNone(films.space_problem(self.library, 500))
            self.assertEqual(films.space_problem(self.library, 501), (films.SPACE_MARGIN + 500, films.SPACE_MARGIN + 501))


class UnitsTests(unittest.TestCase):
    def test_texts(self):
        self.assertEqual(human_size(4_210_000_000), "4.21 GB")
        self.assertEqual(human_size(52_400_000), "52.4 MB")
        self.assertEqual(human_time(None), "estimating…")
        self.assertEqual(human_time(42), "about 42 s left")
        self.assertEqual(human_time(125), "about 2 min 05 s left")
        self.assertEqual(human_time(3 * 3600 + 120), "about 3 h 02 min left")

    def test_eta(self):
        now = [0.0]
        eta = Eta(1000, warmup=1.0, clock=lambda: now[0])
        self.assertEqual(eta.update(0), (None, None))
        now[0] = 1.0
        rate, left = eta.update(100)
        self.assertAlmostEqual(rate, 100)
        self.assertAlmostEqual(left, 9.0)
        now[0] = 2.0
        rate, left = eta.update(300)       # faster now: smoothed, not jumping straight to 200/s
        self.assertTrue(100 < rate < 200)


@unittest.skipUnless(HAVE_AV, "PyAV isn't installed")
class ConverterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = Path(tempfile.mkdtemp())
        cls.ac3 = cls.dir / "ac3 film.mkv"
        cls.video_codec = make_film(cls.ac3, seconds=4, audio_codec="ac3", channels=6, language="slk")
        cls.aac = cls.dir / "aac film.mkv"
        make_film(cls.aac, seconds=2, audio_codec="aac", channels=2)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.dir, ignore_errors=True)

    def run_converter(self, *args):
        """Like the window does: SyncWatch's Python, isolated (-I), JSON lines on stdout."""
        result = subprocess.run([sys.executable, "-I", str(CONVERTER), *map(str, args)], capture_output=True,
                                text=True, timeout=120)
        return result.returncode, [json.loads(line) for line in result.stdout.splitlines() if line.strip()]

    def test_probe(self):
        code, messages = self.run_converter("probe", self.ac3)
        self.assertEqual(code, 0, messages)
        info = messages[0]["probe"]
        self.assertTrue(info["video_ok"])
        self.assertTrue(info["audio_ok"])
        self.assertAlmostEqual(info["duration"], 4.0, delta=0.2)
        audio = [t for t in info["tracks"] if t["type"] == "audio"]
        self.assertEqual((audio[0]["codec"], audio[0]["channels"], audio[0]["language"]), ("ac3", 6, "slk"))

    def test_probe_of_something_that_is_no_film(self):
        junk = self.dir / "junk.mkv"
        junk.write_bytes(b"this is not a film at all" * 100)
        code, messages = self.run_converter("probe", junk)
        self.assertEqual(code, 1)
        self.assertEqual(messages[-1]["error"], "probe")

    def test_convert_ac3_to_aac_picture_untouched(self):
        target = self.dir / "converted.mkv"
        code, messages = self.run_converter("convert", self.ac3, target)
        self.assertEqual(code, 0, messages)
        self.assertEqual(messages[-1]["done"], str(target))
        self.assertTrue(any("progress" in m for m in messages))
        tracks = messages[0]["start"]["tracks"]
        self.assertEqual([t["action"] for t in tracks], ["copy", "to AAC"])
        self.assertFalse(target.with_name(target.name + ".part").exists())
        with av.open(str(target)) as out, av.open(str(self.ac3)) as original:
            self.assertEqual([(s.type, s.codec_context.name) for s in out.streams],
                             [("video", self.video_codec), ("audio", "aac")])
            self.assertEqual(out.streams.audio[0].codec_context.channels, 6)
            self.assertEqual(out.streams.audio[0].metadata.get("language"), "slk")
            self.assertAlmostEqual(out.duration / 1e6, original.duration / 1e6, delta=0.1)   # still lines up
            copied = [bytes(p) for p in out.demux(out.streams.video[0]) if p.size]
            source = [bytes(p) for p in original.demux(original.streams.video[0]) if p.size]
            self.assertEqual(copied, source)   # every picture packet byte for byte
        with av.open(str(target)) as out:
            first_sound = next(iter(out.decode(audio=0)))
            self.assertLess(abs(float(first_sound.pts * first_sound.time_base)), 0.05)  # sound starts on time

    def test_aac_is_kept_unless_all_audio(self):
        target = self.dir / "kept.mkv"
        code, messages = self.run_converter("convert", self.aac, target)
        self.assertEqual(code, 0, messages)
        self.assertEqual([t["action"] for t in messages[0]["start"]["tracks"]], ["copy", "copy"])
        code, messages = self.run_converter("convert", self.aac, self.dir / "all.mkv", "--all-audio")
        self.assertEqual([t["action"] for t in messages[0]["start"]["tracks"]], ["copy", "to AAC"])

    def test_failure_leaves_no_part_file(self):
        target = self.dir / "never.mkv"
        code, messages = self.run_converter("convert", self.dir / "missing.mkv", target)
        self.assertEqual(code, 1)
        self.assertEqual(messages[-1]["error"], "convert")
        self.assertFalse(target.exists() or target.with_name(target.name + ".part").exists())

    def test_usage(self):
        self.assertEqual(convert.main([]), 2)


if __name__ == "__main__":
    unittest.main()
