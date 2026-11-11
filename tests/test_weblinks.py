"""Films from web links (since 0.5.1): which links count, which ones from the room may be opened,
asking the server what's there, downloading (and continuing a stopped download), .torrent links."""
import http.server
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from syncwatch.library import weblinks  # noqa: E402
from syncwatch.library.weblinks import Download, WebError  # noqa: E402

FILM = os.urandom(3 * 1024 * 1024 + 12345)
TORRENT = b"d8:announce0:4:infod4:name8:Film.mkv12:piece lengthi16384e6:pieces20:" + b"x" * 20 + b"ee"


class FilmServer:
    """A web server with one film: answers HEAD, ranges ("Range: bytes=N-"), and can misbehave on purpose."""

    def __init__(self):
        server = self
        self.asked = []                  # (method, path, Range header)
        self.mode = ""                   # "nohead", "norange", "cut", "html", "gone", "login"

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def do_HEAD(self):  # noqa: N802
                server.asked.append(("HEAD", self.path, self.headers.get("Range")))
                if server.mode == "nohead":
                    return self._send(405, b"", head=True)
                self._film(head=True)

            def do_GET(self):  # noqa: N802
                server.asked.append(("GET", self.path, self.headers.get("Range")))
                if self.path.endswith(".torrent"):
                    return self._send(200, TORRENT if "good" in self.path else b"<html>no</html>",
                                      "application/x-bittorrent")
                self._film(head=False)

            def _send(self, status, body, kind="video/x-matroska", head=False, extra=()):
                self.send_response(status)
                self.send_header("Content-Type", kind)
                self.send_header("Content-Length", str(len(body)))
                for name, value in extra:
                    self.send_header(name, value)
                self.end_headers()
                if not head:
                    self.wfile.write(body)

            def _film(self, head):
                if server.mode == "gone":
                    return self._send(404, b"gone", "text/plain", head)
                if server.mode == "login":
                    return self._send(401, b"login", "text/plain", head)
                if server.mode == "html":
                    return self._send(200, b"<html>a page</html>", "text/html; charset=utf-8", head)
                start = 0
                wanted = self.headers.get("Range", "")
                if wanted.startswith("bytes=") and server.mode != "norange":
                    start = int(wanted[6:].split("-")[0])
                body = FILM[start:]
                if server.mode == "norange" or not wanted:
                    status, extra = 200, [("Accept-Ranges", "bytes" if server.mode != "norange" else "none")]
                else:
                    end = start if wanted.endswith(f"{start}-{start}") else len(FILM) - 1
                    body = FILM[start:end + 1]
                    status, extra = 206, [("Content-Range", f"bytes {start}-{end}/{len(FILM)}")]
                extra.append(("Content-Disposition", 'attachment; filename="The Matrix (1999).mkv"'))
                if server.mode == "cut" and not head and start == 0:
                    self.send_response(status)
                    self.send_header("Content-Type", "video/x-matroska")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body[:len(body) // 3])          # ... and then the connection ends
                    self.close_connection = True
                    return
                self._send(status, body, head=head, extra=extra)

        self.http = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base = f"http://127.0.0.1:{self.http.server_address[1]}"
        threading.Thread(target=self.http.serve_forever, daemon=True).start()

    def stop(self):
        self.http.shutdown()
        self.http.server_close()


class ParseTests(unittest.TestCase):
    def test_links(self):
        link = weblinks.parse("  https://abc.download.real-debrid.com/d/XYZ123/Film.2001.1080p.mkv  ")
        self.assertEqual((link.host, link.name, link.torrent),
                         ("abc.download.real-debrid.com", "Film.2001.1080p.mkv", False))
        self.assertEqual(weblinks.parse("http://NAS.lan:8080/films/A%20B.mp4").name, "A B.mp4")
        self.assertTrue(weblinks.parse("https://example.org/get/film.torrent").torrent)
        self.assertEqual(weblinks.parse("https://example.org/").name, "")
        for bad in ("", "film.mkv", "ftp://x/y.mkv", "https://", "https://a b/c", "https://x:99999/",
                    "magnet:?xt=urn:btih:" + "a" * 40, "javascript:alert(1)", "file:///C:/film.mkv",
                    "https://x/" + "a" * 2000, "https://x/\x00.mkv"):
            self.assertIsNone(weblinks.parse(bad), bad)

    def test_links_from_the_room(self):
        for good in ("https://abc.download.real-debrid.com/d/XYZ/Film.mkv", "https://8.8.8.8/film.mkv",
                     "https://example.org:8443/film.mkv"):
            self.assertTrue(weblinks.room_ok(good), good)
        for bad in ("http://example.org/film.mkv", "https://192.168.1.1/film.mkv", "https://10.0.0.5/x",
                    "https://127.0.0.1/x", "https://[::1]/x", "https://localhost/x", "https://nas/x",
                    "https://router.lan/x", "https://box.local/x", "https://me:secret@example.org/x",
                    "https://169.254.1.1/x", "not a link"):
            self.assertFalse(weblinks.room_ok(bad), bad)

    def test_names(self):
        self.assertEqual(weblinks.name_from_disposition('attachment; filename="The Matrix (1999).mkv"'),
                         "The Matrix (1999).mkv")
        self.assertEqual(weblinks.name_from_disposition("attachment; filename*=UTF-8''Am%C3%A9lie.mkv"), "Amélie.mkv")
        self.assertEqual(weblinks.name_from_disposition("inline"), "")
        self.assertEqual(weblinks.clean_name('..\\..\\a/b:c?.mkv'), "_.._a_b_c_.mkv")
        self.assertEqual(weblinks.clean_name("x" * 300 + ".mkv"), "x" * 200)
        link = weblinks.parse("https://example.org/stream/12345")
        self.assertEqual(weblinks.film_name(weblinks.Info("", 0, "video/mp4", True), link), "12345.mp4")
        self.assertEqual(weblinks.film_name(weblinks.Info("", 0, "", True), weblinks.parse("https://example.org/")),
                         "Film from example.org")


class ServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["no_proxy"] = os.environ["NO_PROXY"] = "127.0.0.1,localhost"
        cls.server = FilmServer()

    @classmethod
    def tearDownClass(cls):
        cls.server.stop()

    def setUp(self):
        self.server.mode = ""
        self.server.asked.clear()
        self.folder = Path(tempfile.mkdtemp(prefix="sw-web-"))

    def tearDown(self):
        shutil.rmtree(self.folder, ignore_errors=True)

    # ---- asking what's there
    def test_probe(self):
        info = weblinks.probe(self.server.base + "/d/abc/film")
        self.assertEqual((info.name, info.size, info.kind, info.ranges),
                         ("The Matrix (1999).mkv", len(FILM), "video/x-matroska", True))
        self.assertEqual([method for method, _, _ in self.server.asked], ["HEAD"])

    def test_probe_without_head(self):
        self.server.mode = "nohead"
        info = weblinks.probe(self.server.base + "/film.mkv")
        self.assertEqual((info.size, info.ranges), (len(FILM), True))
        self.assertEqual(self.server.asked[-1], ("GET", "/film.mkv", "bytes=0-0"))

    def test_probe_problems(self):
        for mode, kind in (("gone", "gone"), ("login", "denied"), ("html", "page")):
            self.server.mode = mode
            with self.subTest(mode), self.assertRaises(WebError) as caught:
                weblinks.probe(self.server.base + "/film.mkv")
            self.assertEqual(caught.exception.kind, kind)
        with self.assertRaises(WebError) as caught:
            weblinks.probe("http://127.0.0.1:9/film.mkv")
        self.assertEqual(caught.exception.kind, "network")

    # ---- downloading
    def download(self, name="The Matrix (1999).mkv", size=len(FILM)):
        return Download(self.server.base + "/film.mkv", self.folder, name, size)

    def test_download(self):
        seen = []
        path = self.download().run(lambda have, size: seen.append((have, size)), lambda: False)
        self.assertEqual((path.name, path.read_bytes() == FILM), ("The Matrix (1999).mkv", True))
        self.assertEqual(seen[-1], (len(FILM), len(FILM)))
        self.assertEqual(sorted(p.name for p in self.folder.iterdir()), ["The Matrix (1999).mkv"])
        again = self.download().run(lambda *a: None, lambda: False)      # the name is taken now
        self.assertEqual(again.name, "The Matrix (1999) (2).mkv")

    def test_a_stopped_download_continues_where_it_was(self):
        job = self.download()
        calls = []
        with self.assertRaises(WebError) as caught:
            job.run(lambda *a: None, lambda: calls.append(1) or len(calls) > 2)    # stops after 2 chunks
        self.assertEqual(caught.exception.kind, "stopped")
        saved = job.already()
        self.assertTrue(0 < saved < len(FILM))
        self.server.asked.clear()
        path = self.download().run(lambda *a: None, lambda: False)        # (a new Download, as after a restart)
        self.assertEqual(path.read_bytes(), FILM)
        self.assertEqual(self.server.asked, [("GET", "/film.mkv", f"bytes={saved}-")])

    def test_a_server_that_cant_continue_starts_over(self):
        job = self.download()
        calls = []
        with self.assertRaises(WebError):
            job.run(lambda *a: None, lambda: calls.append(1) or len(calls) > 2)
        self.server.mode = "norange"
        self.assertEqual(self.download().run(lambda *a: None, lambda: False).read_bytes(), FILM)

    def test_another_file_isnt_continued(self):
        job = self.download()
        calls = []
        with self.assertRaises(WebError):
            job.run(lambda *a: None, lambda: calls.append(1) or len(calls) > 2)
        other = self.download(size=len(FILM) + 1)                # same name, another size: not that file
        self.assertEqual(other.already(), 0)

    def test_cut_off_then_continued(self):
        self.server.mode = "cut"
        job = self.download()
        with self.assertRaises(WebError) as caught:
            job.run(lambda *a: None, lambda: False)
        self.assertEqual(caught.exception.kind, "network")
        self.assertTrue(job.already() > 0)
        self.assertEqual(job.run(lambda *a: None, lambda: False).read_bytes(), FILM)   # (the retry continues)

    def test_not_enough_space(self):
        with mock.patch.object(weblinks, "space_problem", return_value=(10, 20)), \
                self.assertRaises(WebError) as caught:
            self.download().run(lambda *a: None, lambda: False)
        self.assertEqual(caught.exception.kind, "disk")

    # ---- .torrent links
    def test_torrent_links(self):
        self.assertEqual(weblinks.fetch_torrent(self.server.base + "/good.torrent"), TORRENT)
        with self.assertRaises(WebError) as caught:
            weblinks.fetch_torrent(self.server.base + "/bad.torrent")
        self.assertEqual(caught.exception.kind, "torrent")


class ProgressTextTests(unittest.TestCase):
    def test_text(self):
        from syncwatch.ui.web_ui import download_text
        self.assertEqual(download_text(50, 100, 0, 10), "⬇ 50 %")
        self.assertIn("left", download_text(500_000_000, 1_000_000_000, 10_000_000, 10))
        self.assertNotIn("left", download_text(500_000_000, 1_000_000_000, 10_000_000, 1))
        self.assertEqual(download_text(100, 100, 5, 10), "⬇ downloaded completely")
        self.assertTrue(download_text(5_000_000, 0, 1_000_000, 10).startswith("⬇ 5"))   # size unknown


if __name__ == "__main__":
    unittest.main()
