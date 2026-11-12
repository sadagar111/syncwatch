"""The log (0.5.2): it can't go silent, and it never keeps a full address.

On Alex's PC the log stopped on 28 Oct 2026: Python's rotating handler drops every line once
Windows refuses to rename the full file (another program has it open). And mpv's own error
lines carried complete YouTube stream addresses, with his IP address in them, into the log
and into "Copy report"."""
import logging
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from syncwatch import errors, log, privacy  # noqa: E402

# from Alex's log of 11 Nov (address shortened and changed, same shape)
MPV_LINE = ("[stream] Failed to open https://rr4---sn-oj5hn5-5m.googlevideo.com/videoplayback?expire=1794454623"
            "&ip=2001%3Adb8%3A3dcd%3A8700%3A4d4e%3A929c%3A77d4%3A3234&itag=303&c=TVHTML5&sig=AE0s2JYwRQIh.")
EDL_LINE = ("Failed to open edl://!new_stream;!no_clip;!no_chapters;%40%https://rr1.googlevideo.com/v?ip=203.0.113.7;"
            "!new_stream;!no_clip;!no_chapters;%38%https://rr2.googlevideo.com/a?ip=203.0.113.7.")


def windows_rename(real):
    """os.rename as on Windows while another program has syncwatch.log open."""
    def rename(src, dst):
        if str(src).endswith("syncwatch.log"):
            raise PermissionError(32, "The process cannot access the file because it is being used by another process")
        return real(src, dst)
    return rename


class PrivacyTests(unittest.TestCase):
    def test_addresses_keep_only_their_host(self):
        self.assertEqual(privacy.hide_addresses(MPV_LINE), "[stream] Failed to open https://rr4---sn-oj5hn5-5m.googlevideo.com/…")
        cleaned = privacy.hide_addresses(EDL_LINE)
        self.assertNotIn("203.0.113.7", cleaned)
        self.assertTrue(cleaned.startswith("Failed to open edl://!new_stream;"))
        cases = {
            "https://abc.download.real-debrid.com/d/SECRET123/Film.mkv": "https://abc.download.real-debrid.com/…",
            "http://127.0.0.1:8123/0123456789abcdef/film.mkv": "http://127.0.0.1:8123/…",
            "https://me:password@nas.example/film.mkv": "https://nas.example/…",
            "https://syncwatch-relay.sadagar1928.workers.dev": "https://syncwatch-relay.sadagar1928.workers.dev",
            "https://example.org/": "https://example.org/",
            "see 'https://x.example/a?b=1' and https://y.example/c": "see 'https://x.example/…' and https://y.example/…",
            "wss://relay.example/v1/channel/abc": "wss://relay.example/…",
        }
        for text, expected in cases.items():
            self.assertEqual(privacy.hide_addresses(text), expected, text)
        self.assertEqual(privacy.hide_addresses("magnet:?xt=urn:btih:abc no address here"),
                         "magnet:?xt=urn:btih:abc no address here")

    def test_keys_too(self):
        self.assertEqual(privacy.clean("api_key=0123456789abcdef https://x.example/k?key=1"),
                         "api_key=*** https://x.example/…")

    def test_error_reports(self):
        report = errors.build("SW-609", technical=MPV_LINE)
        self.assertNotIn("videoplayback", report.technical)
        self.assertNotIn("videoplayback", report.details)
        self.assertIn("googlevideo.com", report.technical)


class LogFileTests(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp(prefix="sw-log-"))
        self.addCleanup(shutil.rmtree, self.folder, True)
        self.addCleanup(log.setup, None)               # leaves no file open (Windows can delete the folder)
        self.logger = logging.getLogger("syncwatch.test")

    def lines(self, name="syncwatch.log"):
        path = self.folder / name
        return path.read_text(encoding="utf-8").splitlines() if path.exists() else []

    def test_addresses_never_reach_the_file_or_the_report(self):
        log.setup(self.folder)
        logging.getLogger("syncwatch.mpv.core").error(MPV_LINE)
        text = "\n".join(self.lines())
        self.assertIn("googlevideo.com/…", text)
        self.assertNotIn("videoplayback", text)
        self.assertNotIn("videoplayback", log.build_report([("Startup checks", MPV_LINE)]))

    def test_it_keeps_writing_when_windows_wont_start_a_new_file(self):
        path = log.setup(self.folder)
        log._file_handler.maxBytes = 2000
        with mock.patch("os.rename", windows_rename(os.rename)):
            for n in range(200):
                self.logger.info("line %03d %s", n, "x" * 40)
        written = self.lines()
        self.assertEqual(len(written), 200)                      # Python's own handler: 39, then silence
        self.assertTrue(written[-1].endswith("line 199 " + "x" * 40))
        state, where, why = log.status()
        self.assertEqual((state, Path(where)), ("growing", path))
        self.assertIn("PermissionError", why)
        # the other program lets go; the next try starts a new file and the state is fine again
        log._file_handler._retry_at = 1.0
        self.logger.info("after")
        self.assertEqual(log.status()[0], "ok")
        self.assertEqual(self.lines(), [self.lines()[0]])        # a fresh file with just that line
        self.assertTrue((self.folder / "syncwatch.log.1").exists())

    def test_a_file_that_cant_be_opened_goes_next_to_it(self):
        real_open = log.SafeRotatingFileHandler._open
        calls = []

        def refuse_first(handler):
            calls.append(handler.baseFilename)
            if len(calls) == 1:
                raise PermissionError(13, "Access is denied")
            return real_open(handler)

        with mock.patch.object(log.SafeRotatingFileHandler, "_open", refuse_first):
            path = log.setup(self.folder)
        self.assertEqual(path.name, f"syncwatch-{os.getpid()}.log")
        self.logger.info("hello")
        self.assertTrue(self.lines(path.name)[-1].endswith("hello"))
        self.assertEqual(log.status()[0], "elsewhere")

    def test_the_startup_check_says_so(self):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from syncwatch.ui import checks  # noqa: PLC0415 - needs Qt
        log.setup(self.folder)
        self.assertEqual((checks.log_check().status, checks.log_check().detail), (checks.OK, "writing syncwatch.log"))
        log._file_handler.maxBytes = 300
        with mock.patch("os.rename", windows_rename(os.rename)):
            for n in range(20):
                self.logger.info("line %d", n)
        result = checks.log_check()
        self.assertEqual((result.status, result.code), (checks.WARN, "SW-113"))
        self.assertIn("keeps growing", result.detail)
        log.setup(None)
        self.assertEqual(checks.log_check().detail, "no log file")


if __name__ == "__main__":
    unittest.main()
