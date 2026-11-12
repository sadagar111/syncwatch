"""tools/youtube_check.py (0.5.2): the ways of asking YouTube, tried against a stand-in.

YouTube itself isn't reachable from the test machines, so yt-dlp's answers are made up here and
point at a local web server: one path answers like a stream server that accepts, another one
refuses with 403 (what YouTube did on Alex's PC on 11 Nov). mpv really opens them."""
import http.server
import os
import shutil
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import youtube_check  # noqa: E402
from syncwatch import mpvlib  # noqa: E402

FILM = (ROOT / "assets" / "selftest" / "selftest.mp4").read_bytes()
VIDEO = "jNQXAC9IVRw"

try:
    mpvlib.load()
    HAVE_MPV = True
except mpvlib.Unavailable:
    HAVE_MPV = False


class StreamServer:
    """/ok/... sends the test film (ranges too), /refused/... answers 403, like YouTube's servers."""

    def __init__(self):
        self.asked = []

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            server_ = self

            def log_message(self, *args):
                pass

            def do_GET(self):  # noqa: N802
                self.server_.asked.append((self.path, self.headers.get("User-Agent"), self.headers.get("X-Test")))
                if self.path.startswith("/refused/"):
                    self.send_response(403)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                start, end = 0, len(FILM) - 1
                wanted = self.headers.get("Range", "")
                if wanted.startswith("bytes="):
                    first, _, last = wanted[6:].partition("-")
                    start = int(first or 0)
                    end = min(int(last), end) if last else end
                body = FILM[start:end + 1]
                self.send_response(206 if wanted else 200)
                self.send_header("Content-Type", "video/mp4")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Accept-Ranges", "bytes")
                if wanted:
                    self.send_header("Content-Range", f"bytes {start}-{end}/{len(FILM)}")
                self.end_headers()
                self.wfile.write(body)

        self.http = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base = f"http://127.0.0.1:{self.http.server_address[1]}"
        threading.Thread(target=self.http.serve_forever, daemon=True).start()

    def stop(self):
        self.http.shutdown()
        self.http.server_close()


def answer(base, picture="ok", sound="ok", client="TVHTML5"):
    """What resolve.py prints, pointing at the stand-in."""
    return {"ok": {"id": VIDEO, "title": "Me at the zoo", "channel": "jawed", "duration": 19.0, "live": False,
                   "thumbnail": "", "height": 240,
                   "video": {"url": f"{base}/{picture}/videoplayback?itag=242&c={client}&ip=203.0.113.9",
                             "codec": "vp9", "height": 240, "fps": 30, "client": client},
                   "audio": {"url": f"{base}/{sound}/videoplayback?itag=251&c={client}&ip=203.0.113.9",
                             "codec": "opus", "client": client},
                   "headers": {"User-Agent": "Mozilla/5.0 (test)", "X-Test": "yes"}, "expires": 4_000_000_000,
                   "captions": [], "warnings": [f"[youtube] {VIDEO}: some formats were skipped, see <address>"]}}


class CheckToolTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["no_proxy"] = os.environ["NO_PROXY"] = "127.0.0.1,localhost"
        cls.server = StreamServer()

    @classmethod
    def tearDownClass(cls):
        cls.server.stop()

    def setUp(self):
        self.data = Path(tempfile.mkdtemp(prefix="sw-ytcheck-"))
        patcher = mock.patch.dict(os.environ, {"SYNCWATCH_DATA_DIR": str(self.data)})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(shutil.rmtree, self.data, True)

    def test_fetching_the_start(self):
        base = self.server.base
        self.assertEqual(youtube_check.fetch_start(f"{base}/ok/v", {"User-Agent": "UA"}, 0), "ok")
        self.assertEqual(youtube_check.fetch_start(f"{base}/ok/v", {}, 4), "ok")
        self.assertEqual(youtube_check.fetch_start(f"{base}/refused/v", {}, 0), "refused (403)")
        self.assertTrue(youtube_check.fetch_start("http://127.0.0.1:9/v", {}, 0).startswith("no connection"))
        self.assertTrue(youtube_check.fetch_start(f"{base}/ok/v", {}, 6).startswith("no connection"))  # (v4 server)
        self.assertIn(("/ok/v", "UA", None), self.server.asked)

    @unittest.skipUnless(HAVE_MPV, "mpv isn't available")
    def test_the_whole_check(self):
        base = self.server.base

        def made_up(video, settings, profile, client, ip):
            if client == "web_safari":
                return {"error": "bot", "message": "Sign in to confirm you're not a bot. https://x.example/a",
                        "warnings": []}
            if ip == 6 or (client == "" and ip == 0):           # 0.5.1's way: YouTube refuses the picture
                return answer(base, picture="refused")
            return answer(base, client="WEB" if client else "TVHTML5")

        with mock.patch.object(youtube_check, "find_streams", made_up), \
                mock.patch.object(youtube_check, "internet", lambda family: True):
            report = youtube_check.check(VIDEO, show=False)
        text = "\n".join(report)
        rows = {line.split("  ")[0]: line for line in report if line.startswith(("as SyncWatch", "the same", "TV", "Safari"))}
        self.assertIn("refused (403)", rows["as SyncWatch 0.5.1 does"])
        self.assertTrue(rows["as SyncWatch 0.5.1 does"].rstrip().endswith("refused (403)"))   # mpv: the same
        self.assertTrue(rows["TV app (tv)"].rstrip().endswith("plays"), rows["TV app (tv)"])
        self.assertIn("240p vp9+opus · WEB", rows["TV app (tv)"])
        self.assertIn("not found: bot", rows["Safari (web_safari)"])
        self.assertTrue(rows["the same, IPv4 only"].rstrip().endswith("-"))            # mpv can't be told IPv4
        self.assertIn("these do: TV app (tv)", text)
        self.assertIn("no connection", rows["the same, IPv6 only"])                     # (the stand-in has no IPv6)
        self.assertIn("Over IPv4 YouTube sends the streams, over IPv6 it didn't work (no connection", text)
        self.assertIn("some formats were skipped", text)                                 # yt-dlp's warnings
        for secret in ("127.0.0.1", "203.0.113.9", "videoplayback", "https://x.example"):
            self.assertNotIn(secret, text)                                               # no addresses at all
        asked = [path for path, ua, extra in self.server.asked]
        self.assertTrue(any(ua == "Mozilla/5.0 (test)" and extra == "yes" for _, ua, extra in self.server.asked))
        self.assertTrue(any(p.startswith("/refused/") for p in asked))

    def test_what_the_result_says(self):
        def row(name, picture="ok", sound="ok", played="plays"):
            return (name, "720p vp9+opus · TVHTML5", picture, sound, played)
        works = youtube_check.verdict([row("as SyncWatch 0.5.1 does")], True, True)
        self.assertIn("works on this PC right now", works[0])
        ipv6 = youtube_check.verdict([row("as SyncWatch 0.5.1 does", "refused (403)", played="refused (403)"),
                                      row("the same, IPv4 only", played="-"),
                                      row("the same, IPv6 only", "refused (403)", "refused (403)", "-")], True, True)
        self.assertIn("Over IPv4 YouTube sends the streams, over IPv6 it refuses them.", ipv6)
        self.assertIn("mpv played none of them, but YouTube sent the streams to Python for: the same, IPv4 only",
                      ipv6[0])
        nothing = youtube_check.verdict([row("as SyncWatch 0.5.1 does", "refused (403)", "refused (403)",
                                             "refused (403)")], True, False)
        self.assertIn("none of the ways worked", nothing[0])
        self.assertIn("no IPv6 connection", " ".join(nothing))

    def test_main_saves_the_report(self):
        with mock.patch.object(youtube_check, "check", lambda video, show=True: ["line one", "line two"]):
            self.assertEqual(youtube_check.main([]), 0)
            self.assertEqual(youtube_check.main(["not a link"]), 2)
        saved = self.data / "profiles" / "default" / "logs" / "youtube_check.txt"
        self.assertEqual(saved.read_text(encoding="utf-8"), "line one\nline two\n")


if __name__ == "__main__":
    unittest.main()
