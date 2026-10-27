"""Tests for subtitle parsing, encodings, timing and reading tracks out of MKV files."""
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from syncwatch.errors import SyncWatchError  # noqa: E402
from syncwatch.subtitles import mkv, parsers  # noqa: E402
from syncwatch.subtitles.model import Cue, SubtitleTrack  # noqa: E402

SLOVAK = "Čo to robíš? Ľúbim ťa, Žofka. Šťastie, ôsmy deň, päť ďalších, Ňa."
CZECH = "Příliš žluťoučký kůň úpěl ďábelské ódy. Řekni mi, kde je Šárka?"
FRENCH = "Où êtes-vous allé? C'était très agréable, à bientôt. Ça va, garçon."


class EncodingTests(unittest.TestCase):
    def check(self, text, encoding, **kwargs):
        decoded, used = parsers.decode(text.encode(encoding), **kwargs)
        self.assertEqual(decoded, text, f"{encoding} decoded as {used}")

    def test_utf8_and_boms(self):
        self.check(SLOVAK, "utf-8")
        decoded, used = parsers.decode(b"\xef\xbb\xbf" + SLOVAK.encode("utf-8"))
        self.assertEqual(decoded, SLOVAK)
        decoded, used = parsers.decode(SLOVAK.encode("utf-16"))
        self.assertEqual(decoded, SLOVAK)

    def test_utf16_without_bom(self):
        decoded, used = parsers.decode(("1\n00:00:01,000 --> 00:00:02,000\n" + SLOVAK).encode("utf-16-le"))
        self.assertIn(SLOVAK, decoded)

    def test_slovak_and_czech_windows_1250(self):
        self.check(SLOVAK, "cp1250", preferred_languages=["sk", "cs", "en"])
        self.check(CZECH, "cp1250", preferred_languages=["sk", "cs", "en"])
        self.check(SLOVAK, "cp1250")  # no preferences at all: Central European is assumed

    def test_slovak_and_czech_iso_8859_2(self):
        self.check(SLOVAK, "iso-8859-2", preferred_languages=["sk"])
        self.check(CZECH, "iso-8859-2", preferred_languages=["cs"])

    def test_french_windows_1252_with_file_name_hint(self):
        self.check(FRENCH, "cp1252", language_hint="fr")
        self.check(FRENCH, "cp1252", preferred_languages=["fr", "en"])

    def test_forced_encoding(self):
        decoded, used = parsers.decode(SLOVAK.encode("cp1250"), forced="cp1250")
        self.assertEqual((decoded, used), (SLOVAK, "cp1250"))


class SrtTests(unittest.TestCase):
    def test_messy_srt(self):
        text = (
            "1\n00:00:01,000 --> 00:00:02,500\n<i>Hello</i> <font color=\"#ff0000\">there</font>\n\n"
            "2\n00:00:03,000 --> 00:00:04,000\n{\\an8}Top line\nsecond\n"
            "3\n00:00:05,5 --> 00:00:06,000\nNo blank line before me\n\n\n"
            "junk line\n"
            "4\n00:00:07.000 --> 00:00:08.000\n<I>Upper</I> &amp; tags\n")
        cues = parsers.parse_srt(text)
        self.assertEqual([c.start for c in cues], [1000, 3000, 5500, 7000])
        self.assertEqual(cues[0].text, "<i>Hello</i> there")
        self.assertTrue(cues[1].top)
        self.assertEqual(cues[1].text, "Top line\nsecond")
        self.assertEqual(cues[2].text, "No blank line before me")
        self.assertEqual(cues[3].text, "<i>Upper</i> & tags")

    def test_empty_and_reversed_cues_dropped(self):
        cues = parsers.parse_srt("1\n00:00:05,000 --> 00:00:04,000\nbackwards\n\n"
                                 "2\n00:00:06,000 --> 00:00:07,000\n\n")
        self.assertEqual(cues, [])


class VttTests(unittest.TestCase):
    def test_vtt(self):
        text = ("WEBVTT - title\n\nNOTE a comment\n\n"
                "intro\n00:01.000 --> 00:02.000 align:center\n<v Bob>Hi <b>you</b></v>\n\n"
                "01:00:00.500 --> 01:00:01.000 line:0\n<c.yellow>Top</c> <00:00:00.700>karaoke\n")
        cues = parsers.parse_vtt(text)
        self.assertEqual(len(cues), 2)
        self.assertEqual(cues[0].text, "Hi <b>you</b>")
        self.assertEqual(cues[1].start, 3_600_500)
        self.assertTrue(cues[1].top)
        self.assertEqual(cues[1].text, "Top karaoke")


ASS = """[Script Info]
Title: test
ScriptType: v4.00+

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Arial,20,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,2,2,2,10,10,10,1
Style: Thoughts,Arial,20,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,0,-1,0,0,100,100,0,0,1,2,2,2,10,10,10,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 0,0:00:01.00,0:00:02.50,Default,,0,0,0,,Line one\\Nline two, with comma
Dialogue: 0,0:00:03.00,0:00:04.00,Thoughts,,0,0,0,,Thinking {\\i0}not italic
Comment: 0,0:00:05.00,0:00:06.00,Default,,0,0,0,,hidden comment
Dialogue: 0,0:00:07.00,0:00:08.00,Default,,0,0,0,,{\\an8\\b1}Bold top{\\b0}
Dialogue: 0,0:00:09.00,0:00:10.00,Default,,0,0,0,,{\\p1}m 0 0 l 100 0 100 100{\\p0}
"""


class AssTests(unittest.TestCase):
    def test_ass(self):
        cues = parsers.parse_ass(ASS)
        self.assertEqual(len(cues), 3)
        self.assertEqual(cues[0].text, "Line one\nline two, with comma")
        self.assertEqual(cues[1].text, "<i>Thinking </i>not italic")
        self.assertTrue(cues[2].top)
        self.assertEqual(cues[2].text, "<b>Bold top</b>")


class TrackLookupTests(unittest.TestCase):
    def setUp(self):
        self.track = SubtitleTrack([Cue(1000, 2000, "a"), Cue(1500, 3000, "b"), Cue(10_000, 11_000, "c")])

    def test_overlapping(self):
        self.assertEqual([c.text for c in self.track.active(1600)], ["a", "b"])
        self.assertEqual(self.track.active(500), [])
        self.assertEqual([c.text for c in self.track.active(2500)], ["b"])

    def test_delay_and_stretch(self):
        self.assertEqual([c.text for c in self.track.active(1200, delay_ms=1000)], [])
        self.assertEqual([c.text for c in self.track.active(2100, delay_ms=1000)], ["a"])
        self.assertEqual([c.text for c in self.track.active(8900, delay_ms=-1000)], [])
        self.assertEqual([c.text for c in self.track.active(9100, delay_ms=-1000)], ["c"])
        stretch = 25 / 23.976
        self.assertEqual([c.text for c in self.track.active(10_000 * stretch + 10, stretch=stretch)], ["c"])

    def test_out_of_order_add(self):
        self.track.add([Cue(5000, 6000, "late"), Cue(0, 100, "early")])
        self.assertEqual(self.track.cues()[0].text, "early")
        self.assertEqual([c.text for c in self.track.active(5500)], ["late"])


class FileTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_find_and_load(self):
        film = self.dir / "Film.2021.1080p.mkv"
        film.write_bytes(b"")
        (self.dir / "Film.2021.1080p.sk.srt").write_bytes(
            ("1\n00:00:01,000 --> 00:00:02,000\n" + SLOVAK + "\n").encode("cp1250"))
        (self.dir / "Film.2021.1080p.eng.srt").write_text("1\n00:00:01,000 --> 00:00:02,000\nHi\n", "utf-8")
        (self.dir / "Subs").mkdir()
        (self.dir / "Subs" / "3_Czech.srt").write_text("1\n00:00:01,000 --> 00:00:02,000\nAhoj\n", "utf-8")
        (self.dir / "unrelated.srt").write_text("x", "utf-8")
        found = dict((p.name, lang) for p, lang in parsers.find_external(film))
        self.assertEqual(found, {"Film.2021.1080p.sk.srt": "sk", "Film.2021.1080p.eng.srt": "en",
                                 "3_Czech.srt": "cs"})
        track = parsers.load_file(self.dir / "Film.2021.1080p.sk.srt", film_stem=film.stem)
        self.assertEqual(track.cues()[0].text, SLOVAK)
        self.assertEqual(track.language, "sk")
        best = parsers.pick_preferred([(p, lang) for p, lang in parsers.find_external(film)], ["cs", "en"])
        self.assertEqual(best.name, "3_Czech.srt")

    def test_bad_files(self):
        empty = self.dir / "empty.srt"
        empty.write_text("nothing here", "utf-8")
        with self.assertRaises(SyncWatchError) as ctx:
            parsers.load_file(empty)
        self.assertEqual(ctx.exception.code, "SW-251")
        with self.assertRaises(SyncWatchError) as ctx:
            parsers.load_file(self.dir / "missing.srt")
        self.assertEqual(ctx.exception.code, "SW-250")

    def test_language_names(self):
        self.assertEqual(parsers.normalise_language("slk"), "sk")
        self.assertEqual(parsers.normalise_language("cs-CZ"), "cs")
        self.assertEqual(parsers.language_from_name(Path("x.Slovak.forced.srt")), "sk")


@unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg not installed")
class MkvTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = Path(tempfile.mkdtemp())
        srt = cls.dir / "sk.srt"
        lines = []
        for i in range(60):
            start = i * 2.0 + 0.5
            lines.append(f"{i + 1}\n{_t(start)} --> {_t(start + 1.2)}\nRiadok {i} – {SLOVAK[:20]}\n")
        srt.write_text("\n".join(lines), encoding="utf-8")
        ass = cls.dir / "en.ass"
        ass.write_text(ASS, encoding="utf-8")
        cls.film = cls.dir / "film.mkv"
        subprocess.run(
            ["ffmpeg", "-loglevel", "error", "-y",
             "-f", "lavfi", "-i", "testsrc=size=160x90:rate=24:duration=125",
             "-f", "lavfi", "-i", "sine=frequency=440:duration=125",
             "-i", str(srt), "-i", str(ass),
             "-map", "0:v", "-map", "1:a", "-map", "2:s", "-map", "3:s",
             "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-c:s:0", "srt", "-c:s:1", "ass",
             "-metadata:s:s:0", "language=slk", "-metadata:s:s:0", "title=Slovenské",
             "-metadata:s:s:1", "language=eng", str(cls.film)], check=True)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.dir, ignore_errors=True)

    def test_track_list(self):
        info = mkv.read_info(self.film)
        self.assertEqual([(t.codec, t.language) for t in info.tracks],
                         [("S_TEXT/UTF8", "sk"), ("S_TEXT/ASS", "en")])
        self.assertEqual(info.tracks[0].name, "Slovenské")
        self.assertAlmostEqual(info.duration_ms / 1000, 125, delta=1)

    def test_extract(self):
        info = mkv.read_info(self.film)
        got = {}
        progress = []
        done = mkv.extract(self.film, info.tracks, lambda n, cues: got.setdefault(n, []).extend(cues),
                           progress=lambda f, eta: progress.append(f))
        self.assertTrue(done)
        sk = got[info.tracks[0].number]
        self.assertEqual(len(sk), 60)
        self.assertEqual(sk[0].start, 500)
        self.assertEqual(sk[0].end, 1700)
        self.assertEqual(sk[59].text, f"Riadok 59 – {SLOVAK[:20]}")
        en = got[info.tracks[1].number]
        self.assertEqual([c.text for c in en],
                         ["Line one\nline two, with comma", "<i>Thinking </i>not italic", "<b>Bold top</b>"])
        self.assertTrue(en[2].top)
        self.assertEqual(progress[-1], 1.0)

    def test_cancel_and_cache(self):
        info = mkv.read_info(self.film)
        cancel = threading.Event()
        cancel.set()
        self.assertFalse(mkv.extract(self.film, info.tracks, lambda n, c: None, cancel=cancel))
        cache_dir = self.dir / "cache"
        mkv.save_cache(cache_dir, self.film, {3: [Cue(1, 2, "x", True)]})
        self.assertEqual(mkv.load_cache(cache_dir, self.film), {3: [Cue(1, 2, "x", True)]})

    def test_not_matroska_and_damaged(self):
        fake = self.dir / "fake.mkv"
        fake.write_bytes(b"\x00" * 100)
        with self.assertRaises(mkv.NotMatroska):
            mkv.read_info(fake)
        broken = self.dir / "broken.mkv"
        data = self.film.read_bytes()
        broken.write_bytes(data[: len(data) // 2])  # cut in half like an unfinished download
        info = mkv.read_info(broken)
        got = []
        mkv.extract(broken, info.tracks, lambda n, cues: got.extend(cues))
        self.assertGreater(len(got), 10)  # what's there is still read

    def test_garbage_does_not_hang(self):
        import random
        rng = random.Random(1)
        data = bytearray(self.film.read_bytes())
        for _ in range(5):
            corrupt = bytearray(data)
            for _ in range(200):
                corrupt[rng.randrange(1000, len(corrupt))] = rng.randrange(256)
            path = self.dir / "corrupt.mkv"
            path.write_bytes(bytes(corrupt))
            try:
                info = mkv.read_info(path)
                mkv.extract(path, info.tracks, lambda n, c: None)
            except (SyncWatchError, mkv.NotMatroska):
                pass


def _t(seconds: float) -> str:
    ms = int(round(seconds * 1000))
    return f"{ms // 3_600_000:02d}:{ms // 60_000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"


if __name__ == "__main__":
    unittest.main()
