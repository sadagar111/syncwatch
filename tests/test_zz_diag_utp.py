"""Temporary, only on branch try/diag-utp (never merged): why does the helper sometimes never connect to a
seeder on this PC? Runs the throttled download (as test_a_jump_is_fetched_first has it) 40 times and the
unthrottled one 5 times, with libtorrent's connection alerts from both sides. Prints DIAG lines; for a trial
that never gets ready it prints everything both sides saw."""
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

import test_torrent as T
from syncwatch.torrent import engine


class DiagHelper(T.Helper):
    """The helper with SYNCWATCH_TORRENT_DEBUG on: its alerts go to a file."""

    def __init__(self, folder: Path, log: Path, *extra):  # noqa: D107
        self.log_file = open(log, "w", encoding="utf-8")
        env = dict(T.LOCAL, SYNCWATCH_TORRENT_DEBUG="1")
        self.proc = subprocess.Popen(engine.command("--folder", folder, "--listen", "127.0.0.1:0", "--dht", "no",
                                                    *extra),
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.log_file,
                                     text=True, env=env)
        self.events = []


@unittest.skipUnless(T.HAVE_LT or T.ON_GITHUB, "libtorrent isn't installed")
class ConnectDiagnosis(unittest.TestCase):
    def trial(self, kbps: int) -> str:
        folder = Path(tempfile.mkdtemp())
        seed = folder / "seed"
        seed.mkdir()
        log = folder / "helper.log"
        lt = T.lt
        try:
            torrent = T.make_torrent(seed, "Film.2001.mkv", os.urandom(16 * 1024 * 1024))
            t0 = time.monotonic()
            seeder = T.Seeder(seed, torrent, kbps)
            cats = lt.alert.category_t
            seeder.session.apply_settings({"alert_mask": cats.status_notification | cats.error_notification
                                           | cats.connect_notification | cats.peer_notification})
            where = (f"seeder {seeder.address} listening={seeder.session.is_listening()} "
                     f"seeding={seeder.handle.status().is_seeding} after {time.monotonic() - t0:.1f} s")
            helper = DiagHelper(folder / "down", log, "--start-mb", "1")
            try:
                helper.send(cmd="add", source=str(torrent))
                files = helper.until("files")
                helper.send(cmd="select", index=engine.best_video(files["files"]))
                started = time.monotonic()
                helper.send(cmd="peer", address=seeder.address)
                try:
                    helper.until("ready", timeout=30)
                    return f"ready after {time.monotonic() - started:.1f} s ({where})"
                except AssertionError as exc:
                    seen = [f"{type(a).__name__}: {a.message()[:160]}" for a in seeder.session.pop_alerts()]
                    helper.quit()
                    helper.log_file.close()
                    lines = log.read_text(encoding="utf-8", errors="replace").splitlines()
                    return ("NO READY in 30 s; " + where + f"; seeder peers {len(seeder.handle.get_peer_info())}"
                            + "\n    SEEDER SAW: " + "\n    SEEDER SAW: ".join(seen[-25:] or ["nothing"])
                            + "\n    HELPER SAW: " + "\n    HELPER SAW: ".join(lines[-40:] or ["nothing"])
                            + "\n    LAST: " + str(exc)[-200:])
            finally:
                helper.quit()
                if not helper.log_file.closed:
                    helper.log_file.close()
        finally:
            shutil.rmtree(folder, ignore_errors=True)

    def test_connecting(self):
        results = []
        for number in range(45):
            kbps = 1500 if number % 9 != 8 else 0         # 40 throttled, 5 not
            result = self.trial(kbps)
            results.append(f"{'throttled' if kbps else 'full-speed'} {number + 1}: {result}")
            print(f"DIAG {results[-1]}", flush=True)
        print(f"DIAG SUMMARY: {sum(r.split(': ', 1)[1].startswith('NO') for r in results)} of {len(results)} "
              "never got ready", flush=True)
