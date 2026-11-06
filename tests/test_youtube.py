"""YouTube watch-along: links, choosing streams, the resolver process, and the stream the player gets.

No test here needs YouTube itself: the resolver runs against a little web server on this PC, and
the choice of streams is checked with yt-dlp's own format selection on a made-up YouTube video.
(tests/youtube_live.py tries the real YouTube on GitHub's machines.)
"""
import functools
import http.server
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from syncwatch.youtube import links, resolve, streams  # noqa: E402

try:
    import yt_dlp
    HAVE_YTDLP = True
except ImportError:
    HAVE_YTDLP = False

ID = "dQw4w9WgXcQ"


class InstalledTests(unittest.TestCase):
    @unittest.skipUnless(os.environ.get("GITHUB_ACTIONS"), "only on GitHub's machines")
    def test_ytdlp_is_installed_on_github(self):
        self.assertTrue(HAVE_YTDLP, "yt-dlp should be installed there (requirements.txt)")


class LinkTests(unittest.TestCase):
    def test_every_kind_of_link(self):
        cases = {
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ": (ID, 0),
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=42s": (ID, 42),
            "https://www.youtube.com/watch?feature=share&v=dQw4w9WgXcQ&list=PL123": (ID, 0),
            "youtube.com/watch?v=dQw4w9WgXcQ&t=1m30s": (ID, 90),
            "https://youtu.be/dQw4w9WgXcQ?t=1h2m3s": (ID, 3723),
            "https://youtu.be/dQw4w9WgXcQ?si=abcdef": (ID, 0),
            "https://www.youtube.com/shorts/dQw4w9WgXcQ": (ID, 0),
            "https://m.youtube.com/watch?v=dQw4w9WgXcQ#t=15": (ID, 15),
            "https://music.youtube.com/watch?v=dQw4w9WgXcQ": (ID, 0),
            "https://www.youtube.com/live/dQw4w9WgXcQ?feature=share": (ID, 0),
            "https://www.youtube-nocookie.com/embed/dQw4w9WgXcQ?start=90": (ID, 90),
            "  dQw4w9WgXcQ  ": (ID, 0),
        }
        for text, (video_id, start) in cases.items():
            with self.subTest(text=text):
                ref = links.parse(text)
                self.assertIsNotNone(ref)
                self.assertEqual((ref.id, ref.start_s), (video_id, start))
                self.assertEqual(ref.url, f"https://www.youtube.com/watch?v={video_id}")

    def test_not_youtube(self):
        for text in ("", "hello", "https://vimeo.com/123456", "https://www.youtube.com/watch?v=short",
                     "https://evil.example/watch?v=dQw4w9WgXcQ", "https://youtube.com.evil.example/watch?v=dQw4w9WgXcQ",
                     "javascript:alert(1)", "file:///C:/Windows/notepad.exe", "https://www.youtube.com/@channel",
                     "https://www.youtube.com/watch?v=dQw4w9WgXcQ--extra", "dQw4w9WgXc!"):
            with self.subTest(text=text):
                self.assertIsNone(links.parse(text))

    def test_find_in_a_message(self):
        ref = links.find("watch this (https://youtu.be/dQw4w9WgXcQ?t=5), it's great")
        self.assertEqual((ref.id, ref.start_s), (ID, 5))
        self.assertIsNone(links.find("no links here"))

    def test_times(self):
        self.assertEqual(links.parse_time("90"), 90)
        self.assertEqual(links.parse_time("2m"), 120)
        self.assertEqual(links.parse_time("junk"), 0)
        self.assertEqual(links.parse_time("999999h"), links.MAX_START_S)


class PlaylistTests(unittest.TestCase):
    """Playlists into the queue (0.5): links, what yt-dlp lists, the checked answer."""

    def test_playlist_links(self):
        cases = {
            "https://www.youtube.com/playlist?list=PLrAXtmErZgOeiKm4sgNOknGvNjby9efdf": ("PLrAXtmErZgOeiKm4sgNOknGvNjby9efdf", None),
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ&list=PLrAXtmErZgOeiKm4sgN&index=3": ("PLrAXtmErZgOeiKm4sgN", ID),
            "youtu.be/dQw4w9WgXcQ?list=OLAK5uy_kXyz1234567890": ("OLAK5uy_kXyz1234567890", ID),
            "https://music.youtube.com/playlist?list=RDCLAK5uy_abcdefghij": ("RDCLAK5uy_abcdefghij", None),
        }
        for link, (list_id, video) in cases.items():
            with self.subTest(link):
                ref = links.parse_playlist(link)
                self.assertEqual(ref.id, list_id)
                self.assertEqual(ref.video.id if ref.video else None, video)
        for link in ("https://www.youtube.com/watch?v=dQw4w9WgXcQ", "https://evil.example/playlist?list=PLabcdefghij",
                     "https://www.youtube.com/playlist?list=short", "https://www.youtube.com/playlist?list=PL<script>xx",
                     "", "javascript:alert(1)//youtube.com/playlist?list=PLabcdefghij"):
            with self.subTest(link):
                self.assertIsNone(links.parse_playlist(link))
        found = links.find_playlist("watch this (https://youtube.com/playlist?list=PLrAXtmErZgOeiKm4sgN)!")
        self.assertEqual(found.id, "PLrAXtmErZgOeiKm4sgN")
        self.assertEqual(found.url, "https://www.youtube.com/playlist?list=PLrAXtmErZgOeiKm4sgN")

    def test_what_ytdlp_lists(self):
        info = {"title": "Mix " + "x" * 300, "entries": [
            {"id": "dQw4w9WgXcQ", "title": "First", "duration": 212},
            {"id": "dQw4w9WgXcQ", "title": "First again"},                       # twice: once
            {"id": "aaaaaaaaaaa", "title": "[Private video]", "duration": None},
            {"id": "bbbbbbbbbbb", "title": "[Deleted video]"},
            {"url": "https://www.youtube.com/embed/ccccccccccc", "title": None},   # no id: from the address
            {"id": "not an id", "url": "https://evil.example/x"},
            "junk", {"id": "ddddddddddd", "duration": True}, {"id": "eeeeeeeeeee", "duration": 1e9}]}
        result = resolve.summarize_playlist(info)
        self.assertEqual([e["id"] for e in result["entries"]], ["dQw4w9WgXcQ", "ccccccccccc", "ddddddddddd",
                                                                 "eeeeeeeeeee"])
        self.assertEqual([e["duration"] for e in result["entries"]], [212.0, 0.0, 0.0, 172800.0])
        self.assertEqual(len(result["title"]), 200)
        many = {"entries": [{"id": f"{n:011d}"} for n in range(80)]}
        self.assertEqual(len(resolve.summarize_playlist(many)["entries"]), resolve.MAX_PLAYLIST)

    def test_the_answer_is_checked(self):
        playlist = streams.playlist_from_json({"playlist": "PLrAXtmErZgOeiKm4sgN", "title": "T",
                                               "entries": [{"id": ID, "title": "A", "duration": "x"},
                                                           {"id": "bad"}, {"id": ID}]})
        self.assertEqual((playlist.id, playlist.title, len(playlist.entries)), ("PLrAXtmErZgOeiKm4sgN", "T", 1))
        self.assertEqual(playlist.entries[0].duration_s, 0.0)
        for bad in ({"playlist": "x"}, {"entries": []}, "list", {"playlist": "PL../../etc/xx"}):
            with self.subTest(bad=bad), self.assertRaises(streams.BadStream):
                streams.playlist_from_json(bad)


class ClassifyTests(unittest.TestCase):
    def test_kinds(self):
        cases = {
            "ERROR: [youtube] x: Sign in to confirm you’re not a bot. Use --cookies": "bot",
            "ERROR: [youtube] x: Sign in to confirm your age. This video may be inappropriate": "signin",
            "ERROR: [youtube] x: Private video. Sign in if you've been granted access": "private",
            "ERROR: [youtube] x: Video unavailable. This video has been removed by the uploader": "unavailable",
            "ERROR: [youtube] x: This live event will begin in 3 hours.": "live",
            "ERROR: [youtube] x: Requested format is not available. Use --list-formats": "outdated",
            "ERROR: [youtube] x: Unable to extract nsig function code": "outdated",
            "ERROR: [youtube] x: Unable to download webpage: <urlopen error [Errno 11001] getaddrinfo failed>": "network",
            "something completely different": "other",
        }
        for text, kind in cases.items():
            with self.subTest(text=text):
                self.assertEqual(resolve.classify(text), kind)


def fake_youtube_video():
    """What yt-dlp's YouTube extractor returns, much shortened: separate picture and sound streams."""
    def fmt(format_id, ext, vcodec, acodec, height=None, fps=None, tbr=None, abr=None, expire="2000000000"):
        entry = {"format_id": format_id, "url": f"https://rr1.googlevideo.example/videoplayback?itag={format_id}"
                 f"&expire={expire}", "ext": ext, "vcodec": vcodec, "acodec": acodec, "protocol": "https",
                 "http_headers": {"User-Agent": "Mozilla/5.0 test", "Accept": "*/*", "Cookie": "never-passed-on"}}
        if height:
            entry.update(height=height, width=height * 16 // 9, fps=fps or 30)
        if tbr:
            entry["tbr"] = tbr
        if abr:
            entry["abr"] = abr
        return entry
    formats = [
        fmt("18", "mp4", "avc1.42001E", "mp4a.40.2", 360, tbr=500),
        fmt("136", "mp4", "avc1.4d401f", "none", 720, tbr=1500),
        fmt("137", "mp4", "avc1.640028", "none", 1080, tbr=4000),
        fmt("248", "webm", "vp9", "none", 1080, tbr=3000),
        fmt("399", "mp4", "av01.0.08M.08", "none", 1080, tbr=2000),
        fmt("271", "webm", "vp9", "none", 1440, tbr=9000),
        fmt("140", "m4a", "none", "mp4a.40.2", abr=129),
        fmt("251", "webm", "none", "opus", abr=135, expire="1900000000"),
    ]
    return {"id": ID, "title": "A test video", "channel": "Test channel", "duration": 212.0,
            "thumbnail": "https://i.ytimg.com/vi/x/hq.jpg", "formats": formats, "extractor": "youtube",
            "extractor_key": "Youtube", "webpage_url": f"https://www.youtube.com/watch?v={ID}",
            "live_status": "not_live"}


@unittest.skipUnless(HAVE_YTDLP, "yt-dlp isn't installed")
class ChoiceTests(unittest.TestCase):
    """yt-dlp's own format selection, with SyncWatch's options, on the made-up video."""

    def choose(self, height=1080, codecs="compatible", combined=False):
        options = resolve.ydl_options(height, codecs, combined, None, None, 10)
        with yt_dlp.YoutubeDL({**options, "logger": _Quiet()}) as ydl:
            info = ydl.process_ie_result(fake_youtube_video(), download=False)
            return resolve.summarize(ydl.sanitize_info(info), now=1_000_000_000)

    def test_picture_and_sound_separately_for_mpv(self):
        found = self.choose()
        self.assertEqual(found["video"]["codec"], "vp9")          # not AV1 ("compatible")
        self.assertEqual(found["video"]["height"], 1080)          # not 1440 (the limit)
        self.assertIn("itag=251", found["audio"]["url"])
        self.assertEqual(found["expires"], 1_900_000_000)          # the earlier of the two
        self.assertEqual(found["headers"]["User-Agent"], "Mozilla/5.0 test")
        self.assertNotIn("Cookie", found["headers"])               # never passed on
        self.assertEqual((found["title"], found["duration"]), ("A test video", 212.0))

    def test_any_codec_and_smaller_limit(self):
        self.assertTrue(self.choose(codecs="any")["video"]["codec"].startswith("av01"))
        self.assertEqual(self.choose(height=720)["video"]["height"], 720)

    def test_one_stream_with_both_for_the_qt_player(self):
        found = self.choose(combined=True)
        self.assertIn("itag=18", found["video"]["url"])
        self.assertIsNone(found["audio"])

    def test_live_is_refused(self):
        live = dict(fake_youtube_video(), live_status="is_live", is_live=True)
        with self.assertRaises(LookupError):
            resolve.summarize(live)


class _Quiet:
    def debug(self, msg): pass
    def info(self, msg): pass
    def warning(self, msg): pass
    def error(self, msg): pass


class StreamTests(unittest.TestCase):
    def found(self, **changes):
        raw = {"id": ID, "title": "T", "channel": "C", "duration": 61.5, "thumbnail": "", "height": 1080,
               "video": {"url": "https://a.example/v?x=1,2;3", "codec": "vp9", "height": 1080, "fps": 30},
               "audio": {"url": "https://a.example/a%2C", "codec": "opus"},
               "headers": {"User-Agent": "UA", "Accept": "*/*"}, "expires": 2_000_000_000}
        raw.update(changes)
        return raw

    def test_edl_joins_picture_and_sound(self):
        stream = streams.from_json(self.found())
        video, audio = "https://a.example/v?x=1,2;3", "https://a.example/a%2C"
        self.assertEqual(stream.address(), f"edl://!new_stream;!no_clip;!no_chapters;%{len(video)}%{video};"
                                           f"!new_stream;!no_clip;!no_chapters;%{len(audio)}%{audio}")
        self.assertEqual(stream.duration_ms, 61500)
        self.assertEqual(stream.header_fields(), ["Accept: */*"])
        self.assertEqual(stream.describe(), "1080p · vp9 + opus")
        self.assertFalse(stream.expired(now=1_000_000_000))
        self.assertTrue(stream.expired(now=2_000_000_000 - 60))

    def test_one_stream_is_opened_directly(self):
        stream = streams.from_json(self.found(audio=None))
        self.assertEqual(stream.address(), "https://a.example/v?x=1,2;3")

    def test_odd_data_is_refused(self):
        for changes in ({"id": "nope"}, {"video": None}, {"video": {"url": "file:///C:/x"}},
                        {"video": {"url": "https://a\r\nInjected: yes"}}, {"duration": "long"}, {"title": 5}):
            with self.subTest(changes=changes), self.assertRaises(streams.BadStream):
                streams.from_json(self.found(**changes))
        stream = streams.from_json(self.found(headers={"Bad\r\nHeader": "x", "X-Ok": "fine", "A:b": "c",
                                                      "X-Line": "a\nb"}))
        self.assertEqual(stream.headers, {"X-Ok": "fine"})


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


def serve(folder: Path):
    """A web server for one folder on 127.0.0.1 (a stand-in for YouTube's stream servers)."""
    handler = functools.partial(_QuietHandler, directory=str(folder))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


@unittest.skipUnless(HAVE_YTDLP, "yt-dlp isn't installed")
class ResolverProcessTests(unittest.TestCase):
    """resolve.py as the window starts it: its own process, one JSON line."""

    @classmethod
    def setUpClass(cls):
        cls.dir = Path(tempfile.mkdtemp())
        (cls.dir / "clip.mkv").write_bytes(b"\x1a\x45\xdf\xa3" + b"\0" * 4096)   # Matroska magic is enough
        cls.server, cls.base = serve(cls.dir)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        shutil.rmtree(cls.dir, ignore_errors=True)

    def run_resolver(self, *args):
        env = dict(os.environ, no_proxy="127.0.0.1,localhost", NO_PROXY="127.0.0.1,localhost")
        result = subprocess.run(resolve.command(*args), capture_output=True, text=True, timeout=120, env=env)
        lines = [line for line in result.stdout.splitlines() if line.strip()]
        self.assertEqual(len(lines), 1, result.stdout + result.stderr)
        return result.returncode, json.loads(lines[0])

    def test_found(self):
        code, message = self.run_resolver(ID, "--test-url", f"{self.base}/clip.mkv", "--timeout", "10")
        self.assertEqual(code, 0, message)
        stream = streams.from_json(message["ok"])
        self.assertEqual(stream.id, ID)
        self.assertEqual(stream.address(), f"{self.base}/clip.mkv")

    def test_a_playlist_through_ytdlp(self):
        """yt-dlp's real flat-playlist path (its generic extractor reads a page with embedded videos)."""
        (self.dir / "list.html").write_text(
            "<html><head><title>Test list</title></head><body>"
            + "".join(f'<iframe src="https://www.youtube.com/embed/{v}"></iframe>' for v in (ID, "abcdefghijk"))
            + "</body></html>", encoding="utf-8")
        code, message = self.run_resolver("PLrAXtmErZgOeiKm4sgN", "--playlist", "--test-url", f"{self.base}/list.html",
                                          "--timeout", "10")
        self.assertEqual(code, 0, message)
        playlist = streams.playlist_from_json(message["ok"])
        self.assertEqual([e.id for e in playlist.entries], [ID, "abcdefghijk"])
        self.assertEqual(playlist.title, "Test list")
        code, message = self.run_resolver("not a list!", "--playlist")
        self.assertEqual((code, message["error"]), (2, "other"))

    def test_not_found(self):
        code, message = self.run_resolver(ID, "--test-url", f"{self.base}/missing.mkv", "--timeout", "10")
        self.assertEqual(code, 1)
        self.assertEqual(message["error"], "unavailable", message)

    def test_only_video_ids(self):
        code, message = self.run_resolver("https://evil.example/x")
        self.assertEqual(code, 2)
        self.assertIn("error", message)

    def test_flags_keep_user_libraries(self):
        self.assertNotIn("-I", resolve.command()[1:-1])


if __name__ == "__main__":
    unittest.main()


class ProtocolTests(unittest.TestCase):
    """The room messages carry a YouTube video's ID in "film": checked like everything else."""

    def body(self, **film):
        return {"t": "status", "s": "0" * 16, "n": 1, "pos": 0, "playing": False, "at": 0,
                "film": {"name": "A video", "size": 0, "dur": 1000, **film}}

    def test_video_id(self):
        from syncwatch.net import protocol
        clean = protocol.validate(protocol.ROOM, self.body(yt=ID))
        self.assertEqual(clean["film"]["yt"], ID)
        self.assertNotIn("yt", protocol.validate(protocol.ROOM, self.body())["film"])
        for bad in ("short", "dQw4w9WgXcQ1", "dQw4w9WgXc!", "../../etc/pa", 12345678901, ["x"], "dQw4w9WgXc\n"):
            with self.subTest(bad=bad), self.assertRaises(protocol.BadMessage):
                protocol.validate(protocol.ROOM, self.body(yt=bad))


class QueueTests(unittest.TestCase):
    """The queue on its own: no room, no Qt."""

    def setUp(self):
        from syncwatch.youtube.queue import Queue
        self.queue = Queue()
        self.a = self.queue.add("aaaaaaaaaaa", "A", 60_000, "Alex")
        self.b = self.queue.add("bbbbbbbbbbb", "B", 0, "Marek")
        self.c = self.queue.add("ccccccccccc", "C", 30_000, "Kuba")

    def order(self, queue=None):
        return [item.id[0] for item in (queue or self.queue).items]

    def test_order_and_playing(self):
        self.assertEqual(self.order(), ["a", "b", "c"])
        self.assertTrue(self.queue.move(self.c.key, 0))
        self.assertEqual(self.order(), ["c", "a", "b"])
        self.assertEqual(self.queue.advance().id[0], "c")
        self.assertEqual(self.order(), ["a", "b"])
        self.assertEqual(self.queue.play(self.b.key).id[0], "b")
        self.assertEqual((self.queue.now.id[0], self.order()), ("b", ["a"]))
        self.assertEqual(self.queue.remove(self.a.key).id[0], "a")
        self.assertIsNone(self.queue.advance())                     # nothing left: "now" stays
        self.assertEqual(self.queue.now.id[0], "b")
        self.assertFalse(self.queue.move(999, 0))

    def test_limits(self):
        from syncwatch.youtube.queue import MAX_ITEMS, QueueFull
        with self.assertRaises(ValueError):
            self.queue.add("not an id", "x", 0, "x")
        for index in range(MAX_ITEMS - 3):
            self.queue.add(f"v{index:010d}", "x" * 500, 0, "y" * 99)
        with self.assertRaises(QueueFull):
            self.queue.add("zzzzzzzzzzz", "Z", 0, "Z")
        self.assertEqual(len(self.queue.items[-1].title), 120)

    def test_titles_and_lengths_come_later(self):
        self.assertTrue(self.queue.update_info("bbbbbbbbbbb", "B (real title)", 125_000))
        self.assertEqual((self.b.title, self.b.dur), ("B (real title)", 125_000))
        self.assertTrue(self.queue.update_info("aaaaaaaaaaa", "A2", 0))     # unknown length: kept
        self.assertEqual(self.a.dur, 60_000)
        self.assertFalse(self.queue.update_info("aaaaaaaaaaa", "A2", 0))

    def test_members_copy_and_old_messages(self):
        from syncwatch.net import protocol
        from syncwatch.youtube.queue import Queue
        self.queue.advance()
        body = {"t": "queue", "s": "0" * 16, "n": 1, **self.queue.to_message(),
                "rules": {"add": "everyone", "control": "host"}}
        clean = protocol.validate(protocol.ROOM, body)      # what the host sends passes the checks
        member = Queue()
        self.assertTrue(member.load_message(clean))
        self.assertEqual((member.now.id[0], self.order(member)), ("a", ["b", "c"]))
        old = dict(clean, no=clean["no"] - 1, items=[])
        self.assertFalse(member.load_message(old))           # an older message doesn't win
        self.assertEqual(self.order(member), ["b", "c"])
        self.assertIsNone(member.items[0].by_id)             # who added it, by key: the host's app only
