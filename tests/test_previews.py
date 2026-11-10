"""Pictures over the seek bar (0.5): which moments, making them in their own process, reading them."""
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from syncwatch.library import previews  # noqa: E402

try:
    import av  # noqa: F401
    from tests.media_maker import make_film
except ImportError:          # pragma: no cover - PyAV is in the runtime and on GitHub
    make_film = None


class PlanTests(unittest.TestCase):
    def test_moments(self):
        self.assertEqual(previews.plan(20, 10, 400), [5.0, 15.0])
        self.assertEqual(len(previews.plan(7200, 10, 400)), 400)              # long films: spread out
        self.assertEqual(previews.plan(7200, 10, 400)[:2], [9.0, 27.0])
        self.assertEqual(previews.plan(3, 10, 400), [1.5])                    # shorter than a step: one
        for nothing in ((0, 10, 400), (-5, 10, 400), (100, 0, 400), (100, 10, 0)):
            self.assertEqual(previews.plan(*nothing), [])


@unittest.skipIf(make_film is None, "needs PyAV")
class MakeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = Path(tempfile.mkdtemp(prefix="sw-previews-"))
        cls.film = cls.dir / "film.mkv"
        make_film(cls.film, seconds=12)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.dir, ignore_errors=True)

    def run_maker(self, *args):
        result = subprocess.run(previews.command(*args), capture_output=True, text=True, timeout=120)
        lines = [json.loads(line) for line in result.stdout.splitlines() if line.startswith("{")]
        return result.returncode, lines

    def test_pictures_of_a_film(self):
        out = self.dir / "film.swpv"
        code, lines = self.run_maker(self.film, out, "--every", "2", "--width", "96")
        self.assertEqual(code, 0, lines)
        self.assertEqual(lines[0]["start"]["count"], 6)
        self.assertEqual(lines[-1], {"done": str(out)})
        self.assertFalse(Path(str(out) + ".part").exists())
        file = previews.PreviewFile(out)
        self.assertEqual((file.count, file.width, file.step), (6, 96, 2.0))
        self.assertEqual(file.height % 2, 0)
        for index in range(file.count):
            self.assertTrue(file.picture(index).startswith(b"\xff\xd8\xff"))       # each one a JPEG
        self.assertEqual(file.index_at(0), 0)
        self.assertEqual(file.index_at(5.9), 2)
        self.assertEqual(file.index_at(999), 5)
        self.assertIsNone(file.picture(6))

    def test_not_a_film(self):
        junk = self.dir / "junk.mkv"
        junk.write_bytes(b"not a film at all" * 100)
        code, lines = self.run_maker(junk, self.dir / "junk.swpv")
        self.assertEqual(code, 1)
        self.assertIn("error", lines[-1])
        self.assertFalse((self.dir / "junk.swpv").exists())

    def test_a_damaged_file_is_refused(self):
        out = self.dir / "damaged.swpv"
        self.run_maker(self.film, out, "--every", "4")
        data = out.read_bytes()
        for broken in (data[:-10], b"XXXX" + data[4:], previews.MAGIC + b"{" * 70000,
                       data.replace(b'"sizes": [', b'"sizes": [-1, ', 1)):
            out.write_bytes(broken)
            with self.assertRaises((ValueError, KeyError)):
                previews.PreviewFile(out)


if __name__ == "__main__":
    unittest.main()
