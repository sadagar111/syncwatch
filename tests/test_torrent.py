"""Streaming torrents: links, the helper process against a seeder on this PC, and mpv playing it.

No internet: every test makes its own torrent from a local file and seeds it from a libtorrent
session in this process (only on 127.0.0.1, no DHT), then tells the helper to connect to it.
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
import warnings
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
if sys.platform.startswith("linux"):
    os.environ.setdefault("QT_AUDIO_BACKEND", "pulseaudio")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from syncwatch.torrent import engine, links  # noqa: E402

try:
    import libtorrent as lt
    HAVE_LT = True
except ImportError:
    HAVE_LT = False
ON_GITHUB = bool(os.environ.get("GITHUB_ACTIONS"))
HASH = "4b0c7aed0cff8b7804a5955b9d68b712369c4792"
LOCAL = dict(os.environ, no_proxy="127.0.0.1,localhost", NO_PROXY="127.0.0.1,localhost")


class InstalledTests(unittest.TestCase):
    @unittest.skipUnless(ON_GITHUB, "only on GitHub's machines")
    def test_libtorrent_is_installed_on_github(self):
        self.assertTrue(HAVE_LT, "libtorrent should be installed there (requirements.txt)")


class LinkTests(unittest.TestCase):
    def test_magnets(self):
        magnet = f"magnet:?xt=urn:btih:{HASH.upper()}&dn=Big+Buck+Bunny&tr=udp%3A%2F%2Ftracker.example%3A80"
        self.assertEqual(links.magnet_hash(magnet), HASH)
        self.assertEqual(links.magnet_name(magnet), "Big Buck Bunny")
        self.assertEqual(links.magnet_hash("magnet:?xt=urn:btih:MFRGGZDFMZTWQ2LKNNWG23TPOBYXE43U"),
                         "mfrggzdfmztwq2lknnwg23tpobyxe43u")                     # base32 form
        self.assertTrue(links.is_magnet("magnet:?xt=urn:btmh:1220" + "ab" * 32))   # BitTorrent v2
        for bad in ("", "magnet:?dn=nothing", "magnet:?xt=urn:btih:123", "http://example.com/x.torrent",
                    f"magnet:?xt=urn:btih:{HASH}\nInjected", "x" * 9000):
            self.assertFalse(links.is_magnet(bad), bad[:40])
        self.assertEqual(links.find_magnet(f"look: <magnet:?xt=urn:btih:{HASH}> ok"), f"magnet:?xt=urn:btih:{HASH}")

    def test_torrent_files(self):
        folder = Path(tempfile.mkdtemp())
        good, empty, other = folder / "a.torrent", folder / "b.torrent", folder / "c.txt"
        good.write_bytes(b"d4:infod4:name1:xee")
        empty.write_bytes(b"")
        other.write_bytes(b"x")
        self.assertEqual([links.is_torrent_file(p) for p in (good, empty, other, folder / "missing.torrent")],
                         [True, False, False, False])
        shutil.rmtree(folder)

    def test_addresses(self):
        good = ("203.0.113.5:6881", "[2001:db8::1]:50000", "192.168.1.20:1")
        bad = ("127.0.0.1:6881", "0.0.0.0:1", "203.0.113.5:0", "203.0.113.5:70000", "example.com:80", "1.2.3.4",
               "224.0.0.1:5000", "[::1]:80", "")
        self.assertEqual([links.valid_address(a) for a in good], [True] * len(good))
        self.assertEqual([links.valid_address(a) for a in bad], [False] * len(bad))

    def test_choosing_the_film(self):
        files = [{"index": 0, "path": "Film/sample.mkv", "size": 20_000_000},
                 {"index": 1, "path": "Film/Film.2001.mkv", "size": 4_000_000_000},
                 {"index": 2, "path": "Film/Film.nfo", "size": 9_000_000_000}]
        self.assertEqual(engine.best_video(files), 1)
        self.assertIsNone(engine.best_video([files[2]]))


def make_torrent(folder: Path, name: str, data: bytes, piece: int = 65536) -> Path:
    (folder / name).write_bytes(data)
    with warnings.catch_warnings():   # the newer way of making torrents isn't in every version yet
        warnings.simplefilter("ignore")
        storage = lt.file_storage()
        lt.add_files(storage, str(folder / name))
        maker = lt.create_torrent(storage, piece)
        lt.set_piece_hashes(maker, str(folder))
    path = folder / (name + ".torrent")
    path.write_bytes(lt.bencode(maker.generate()))
    return path


class Seeder:
    """A libtorrent session in this process that shares one file on 127.0.0.1."""

    def __init__(self, folder: Path, torrent: Path, upload_kbps: int = 0, port: int = 0):
        settings = {"listen_interfaces": f"127.0.0.1:{port}", "enable_dht": False, "enable_lsd": False,
                    "enable_upnp": False, "enable_natpmp": False, "upload_rate_limit": upload_kbps * 1024}
        if upload_kbps:
            # A limited upload over uTP runs far below its limit (libtorrent's limiter and uTP's congestion control
            # work against each other: the first MB took 6 s instead of 0.7 s here, and on Windows it sometimes
            # didn't come at all in 30 s). Over TCP the limit is what it says. The helper tries uTP first, gets no
            # answer and switches to TCP by itself, about 9 s later.
            settings.update(enable_incoming_utp=False, enable_outgoing_utp=False)
        self.session = lt.session(settings)
        if upload_kbps:   # peers on this PC normally aren't limited: put everyone in the global class
            classes = lt.ip_filter()
            classes.add_rule("0.0.0.0", "255.255.255.255", 1 << lt.session.global_peer_class_id)
            self.session.set_peer_class_filter(classes)
        params = lt.add_torrent_params()
        params.ti = lt.torrent_info(str(torrent))
        params.save_path = str(folder)
        self.handle = self.session.add_torrent(params)
        deadline = time.monotonic() + 20
        while not self.handle.status().is_seeding and time.monotonic() < deadline:
            time.sleep(0.05)

    @property
    def address(self) -> str:
        return f"127.0.0.1:{self.session.listen_port()}"


class Helper:
    """engine.py as SyncWatch runs it: its own process, JSON lines both ways."""

    def __init__(self, folder: Path, *extra):
        self.proc = subprocess.Popen(engine.command("--folder", folder, "--listen", "127.0.0.1:0", "--dht", "no",
                                                    *extra),
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                     text=True, env=LOCAL)
        self.events = []

    def send(self, **command):
        self.proc.stdin.write(json.dumps(command) + "\n")
        self.proc.stdin.flush()

    def until(self, kind, timeout=30.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            line = self.proc.stdout.readline()
            if not line:
                break
            event = json.loads(line)
            self.events.append(event)
            if event["ev"] == kind:
                return event
            if event["ev"] == "error" and kind != "error":
                raise AssertionError(f"helper error: {event}")
        last = next((e for e in reversed(self.events) if e["ev"] == "status"), None)
        raise AssertionError(f"no {kind} event; got {[e['ev'] for e in self.events][-10:]}; last status: {last}")

    def quit(self, delete=False):
        if self.proc.poll() is None:
            self.send(cmd="quit", delete=delete)
            self.proc.wait(timeout=20)
        for pipe in (self.proc.stdin, self.proc.stdout):
            if pipe is not None and not pipe.closed:
                pipe.close()


def read(url, start=None, end=None, timeout=30):
    headers = {"Range": f"bytes={start}-{'' if end is None else end}"} if start is not None else {}
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=timeout) as answer:
        return answer.status, answer.read()


@unittest.skipUnless(HAVE_LT or ON_GITHUB, "libtorrent isn't installed")
class EngineTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.seed_dir, self.down = self.dir / "seed", self.dir / "down"
        self.seed_dir.mkdir()
        self.helper = None

    def tearDown(self):
        if self.helper is not None:
            self.helper.quit()
        shutil.rmtree(self.dir, ignore_errors=True)

    def stream(self, data, upload_kbps=0, name="Film.2001.mkv", *extra):
        torrent = make_torrent(self.seed_dir, name, data)
        self.seeder = Seeder(self.seed_dir, torrent, upload_kbps)
        self.helper = Helper(self.down, *extra)
        self.helper.send(cmd="add", source=str(torrent))
        files = self.helper.until("files")
        self.helper.send(cmd="select", index=engine.best_video(files["files"]))
        self.helper.send(cmd="peer", address=self.seeder.address)
        return self.helper.until("ready")

    def test_plays_while_it_downloads_and_every_byte_is_right(self):
        data = os.urandom(6 * 1024 * 1024 + 12345)
        ready = self.stream(data, 0, "Film.2001.mkv", "--start-mb", "1")
        self.assertTrue(ready["url"].startswith("http://127.0.0.1:"))
        self.assertEqual((ready["size"], ready["name"]), (len(data), "Film.2001.mkv"))
        status, body = read(ready["url"], 3_000_000, 3_065_535)
        self.assertEqual((status, body), (206, data[3_000_000:3_065_536]))
        status, body = read(ready["url"], None, None, timeout=60)
        self.assertEqual((status, body), (200, data))
        self.helper.until("done")
        with self.assertRaises(urllib.error.HTTPError):                       # the secret token counts
            read(ready["url"].replace(ready["url"].split("/")[3], "wrong-token"), 0, 10)

    def test_an_address_from_the_room_is_tried_again(self):
        """Nobody listens at the address yet (a router in the way, or the other side still starting): it's
        tried again every few seconds, so the film comes as soon as the seeder is there."""
        data = os.urandom(2 * 1024 * 1024)
        torrent = make_torrent(self.seed_dir, "Film.2001.mkv", data)
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()
        self.helper = Helper(self.down, "--start-mb", "1")
        self.helper.send(cmd="add", source=str(torrent))
        files = self.helper.until("files")
        self.helper.send(cmd="select", index=engine.best_video(files["files"]))
        self.helper.send(cmd="peer", address=f"127.0.0.1:{port}")      # refused: nobody there yet
        time.sleep(7)       # libtorrent alone gives such an address up after three quick tries
        self.seeder = Seeder(self.seed_dir, torrent, 0, port)
        started = time.monotonic()
        self.helper.until("ready", timeout=25)
        self.assertLess(time.monotonic() - started, 20)

    def test_a_jump_is_fetched_first(self):
        """The seeder sends 1.5 MB/s: the whole film takes ~10 s, but a jump near the end takes ~1 s."""
        data = os.urandom(16 * 1024 * 1024)
        ready = self.stream(data, 1500, "Film.2001.mkv", "--start-mb", "1")
        started = time.monotonic()
        _status, body = read(ready["url"], 12_000_000, 12_262_143)
        took = time.monotonic() - started
        self.assertEqual(body, data[12_000_000:12_262_144])
        self.assertLess(took, 4.0, "the pieces at the jump should have been fetched first")
        status = self.helper.until("status")
        self.assertLess(status["progress"], 0.9, "it shouldn't have needed the whole film")

    def test_streaming_only_deletes_the_film(self):
        data = os.urandom(2 * 1024 * 1024)
        self.stream(data, 0, "Film.2001.mkv", "--start-mb", "1")
        self.helper.until("done")
        self.assertTrue((self.down / "Film.2001.mkv").exists())
        self.helper.quit(delete=True)
        self.assertFalse((self.down / "Film.2001.mkv").exists())

    def test_bad_sources_and_torrents_without_a_film(self):
        self.helper = Helper(self.down)
        self.helper.send(cmd="add", source="magnet:?xt=urn:btih:nonsense")
        self.assertEqual(self.helper.until("error")["kind"], "bad_source")
        self.helper.quit()
        torrent = make_torrent(self.seed_dir, "notes.txt", b"no film here" * 1000)
        self.helper = Helper(self.down)
        self.helper.send(cmd="add", source=str(torrent))
        self.assertEqual(self.helper.until("error")["kind"], "no_video")


@unittest.skipUnless(HAVE_LT or ON_GITHUB, "libtorrent isn't installed")
class SharingTests(unittest.TestCase):
    """A film on one PC, shared as a private torrent, streamed by another helper."""

    def test_share_and_fetch(self):
        folder = Path(tempfile.mkdtemp())
        try:
            (folder / "mine").mkdir()
            film = folder / "mine" / "Our Film.mkv"
            data = os.urandom(3 * 1024 * 1024 + 777)
            film.write_bytes(data)
            sharer = Helper(folder / "unused", "--cache", folder / "cache")
            sharer.send(cmd="share", path=str(film))
            hashing = sharer.until("hashing")
            shared = sharer.until("shared")
            addresses = sharer.until("addresses")
            self.assertGreater(hashing["total"], 0)
            self.assertEqual((shared["name"], shared["size"]), ("Our Film.mkv", len(data)))
            self.assertTrue(links.is_magnet(shared["magnet"]))
            port = addresses["list"][0].rsplit(":", 1)[1] if addresses["list"] else None
            self.assertIsNotNone(port, "the sharer should know at least one of its addresses")
            fetcher = Helper(folder / "down", "--start-mb", "1")
            fetcher.send(cmd="add", source=shared["magnet"], private=True)
            fetcher.send(cmd="peer", address=f"127.0.0.1:{port}")
            files = fetcher.until("files")        # the description comes from the sharer (private torrent)
            fetcher.send(cmd="select", index=files["files"][0]["index"])
            ready = fetcher.until("ready")
            self.assertEqual(read(ready["url"], None, None, timeout=60)[1], data)
            fetcher.quit()
            sharer.quit()
            self.assertTrue(film.is_file(), "sharing never deletes the film")
            again = Helper(folder / "unused", "--cache", folder / "cache")   # the second time: no hashing
            again.send(cmd="share", path=str(film))
            self.assertEqual(again.until("shared")["magnet"], shared["magnet"])
            self.assertNotIn("hashing", [e["ev"] for e in again.events])
            again.quit()
        finally:
            shutil.rmtree(folder, ignore_errors=True)


@unittest.skipUnless((HAVE_LT or ON_GITHUB), "libtorrent isn't installed")
class PlayerTests(unittest.TestCase):
    """mpv plays the film from the helper's address while it downloads, and can jump in it."""

    def test_mpv_plays_and_jumps(self):
        from syncwatch import mpvlib
        try:
            mpvlib.load()
        except mpvlib.Unavailable:
            if ON_GITHUB:
                raise
            self.skipTest("libmpv isn't installed")
        from PySide6.QtWidgets import QApplication
        from syncwatch.ui.mpv_player import MpvPlayer
        from syncwatch.ui.torrent_ui import TorrentSource
        app = QApplication.instance() or QApplication([])

        def spin(seconds, until):
            end = time.monotonic() + seconds
            while time.monotonic() < end:
                app.processEvents()
                if until():
                    return True
                time.sleep(0.01)
            return False

        folder = Path(tempfile.mkdtemp())
        seed_dir = folder / "seed"
        seed_dir.mkdir()
        torrent = make_torrent(seed_dir, "demo.mkv", (ROOT / "tests" / "media" / "demo.mkv").read_bytes(), 16384)
        seeder = Seeder(seed_dir, torrent, 300)        # slower than the film needs at first: it must wait
        helper = Helper(folder / "down", "--start-mb", "1")
        player = MpvPlayer(mpvlib.load(), "none")
        player.video_widget("#000000")
        player.start()
        try:
            helper.send(cmd="add", source=str(torrent))
            files = helper.until("files")
            helper.send(cmd="select", index=files["files"][0]["index"])
            helper.send(cmd="peer", address=seeder.address)
            ready = helper.until("ready")
            source = TorrentSource(files["hash"], ready["name"], ready["url"], ready["size"])
            self.assertTrue(player.load_stream(source))
            self.assertTrue(spin(30, player.is_ready), "the film never opened")
            self.assertAlmostEqual(player.duration(), 40_023, delta=300)
            player.seek(30_000)
            self.assertTrue(spin(30, player.is_ready), "the jump never finished")
            self.assertAlmostEqual(player.position(), 30_000, delta=200)
            player.play()
            self.assertTrue(spin(10, lambda: player.position() > 31_000))
        finally:
            player.shutdown()
            helper.quit()
            shutil.rmtree(folder, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
