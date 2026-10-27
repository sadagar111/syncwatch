"""Two SyncWatch apps talking over a real network connection on this PC, with fake players.

The fake player behaves like a real one in the ways that matter for sync:
after a jump it needs a moment before it's ready (``seek_delay``) and doesn't
move during that time, and its position can be made to drift silently.
"""
import json
import queue
import socket
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from syncwatch import PROTOCOL_VERSION  # noqa: E402
from syncwatch.net import link as link_module  # noqa: E402
from syncwatch.settings import Settings  # noqa: E402
from syncwatch.sync.controller import PAUSE_ALIGN_MS, Controller  # noqa: E402

PORT = 50611


class FakePlayer:
    def __init__(self, seek_delay=0.05, duration=7_200_000):
        self.base, self.base_time, self.ready_at = 0.0, time.monotonic(), 0.0
        self.playing = False
        self.seek_delay = seek_delay
        self.dur = duration
        self.seeks = 0

    def position(self):
        if not self.playing:
            return self.base
        now = time.monotonic()
        return self.base + max(0.0, now - max(self.base_time, self.ready_at)) * 1000

    def duration(self):
        return self.dur

    def is_playing(self):
        return self.playing

    def is_ready(self):
        return time.monotonic() >= self.ready_at

    def has_media(self):
        return True

    def play(self):
        if not self.playing:
            self.base, self.base_time, self.playing = self.position(), time.monotonic(), True

    def pause(self):
        self.base, self.playing = self.position(), False

    def seek(self, ms):
        self.base, self.base_time = float(ms), time.monotonic()
        self.ready_at = time.monotonic() + self.seek_delay
        self.seeks += 1

    def drift(self, ms):
        self.base += ms


class RecorderUI:
    def __init__(self):
        self.events = []
        self.countdowns = []

    def status(self, text, connected):
        self.events.append(("status", text))

    def activity(self, text, kind="info"):
        self.events.append(("activity", text))

    def problem(self, code, technical="", **params):
        self.events.append(("problem", code))

    def countdown(self, ms):
        self.countdowns.append(ms)

    def changed(self):
        pass

    def problems(self):
        return [e[1] for e in self.events if e[0] == "problem"]


def make_settings(**values):
    settings = Settings(None)
    settings.set("sync.port", PORT)
    settings.set("sync.countdown_s", 1)
    settings.set("sync.seek_debounce_ms", 300)
    for key, value in values.items():
        settings.set(key.replace("__", "."), value)
    return settings


class Pair:
    """A host and a friend connected to each other."""

    def __init__(self, host_settings=None, friend_settings=None, friend_seek_delay=0.05):
        self.qh, self.qf = queue.Queue(), queue.Queue()
        self.ph, self.pf = FakePlayer(), FakePlayer(seek_delay=friend_seek_delay)
        self.uh, self.uf = RecorderUI(), RecorderUI()
        self.h = Controller(self.ph, self.uh, host_settings or make_settings(), self.qh.put)
        self.f = Controller(self.pf, self.uf, friend_settings or make_settings(), self.qf.put)
        self.h.set_profile("Alex", None)
        self.f.set_profile("Marek", None)

    def connect(self):
        assert self.h.host()
        assert self.f.join("127.0.0.1")
        self.pump(1.0)
        assert self.h.connected and self.f.connected, (self.uh.events, self.uf.events)

    def pump(self, seconds):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            for q, c in ((self.qh, self.h), (self.qf, self.f)):
                while not q.empty():
                    c.handle(q.get())
                c.tick()
            time.sleep(0.01)

    def gap(self):
        return self.ph.position() - self.pf.position()

    def close(self):
        self.f.leave()
        self.h.leave()
        time.sleep(0.05)


class SyncTests(unittest.TestCase):
    def setUp(self):
        self.pair = Pair()

    def tearDown(self):
        self.pair.close()

    def test_join_aligns_to_host(self):
        p = self.pair
        p.ph.seek(600_000)
        p.connect()
        p.pump(1.5)
        self.assertLess(abs(p.gap()), 60)
        self.assertFalse(p.pf.playing)
        self.assertIn(("activity", "Marek joined"), p.uh.events)

    def test_play_pause_from_both_sides(self):
        p = self.pair
        p.connect()
        p.h.play()
        p.pump(0.5)
        self.assertTrue(p.pf.playing)
        self.assertLess(abs(p.gap()), 80)
        p.f.pause()                       # friend's request goes through the host
        p.pump(0.5)
        self.assertFalse(p.ph.playing)
        self.assertFalse(p.pf.playing)
        # Both stop within PAUSE_ALIGN_MS of each other: closer than that isn't worth a jump.
        # (On Linux this is ~0 ms; on Windows the clock ticks every ~16 ms, so ~10 ms is normal.)
        self.assertLess(abs(p.gap()), PAUSE_ALIGN_MS)

    def test_friend_play_pause_cooldown(self):
        p = self.pair
        p.connect()
        p.f.play()
        p.pump(0.3)
        p.f.pause()                       # within the 2 s cooldown: refused locally
        p.pump(0.3)
        self.assertTrue(p.ph.playing)
        self.assertIn("SW-502", p.uf.problems())
        p.pump(2.0)
        p.f.pause()
        p.pump(0.5)
        self.assertFalse(p.ph.playing)

    def test_host_has_no_cooldown(self):
        p = self.pair
        p.connect()
        for _ in range(3):
            p.h.play()
            p.pump(0.1)
            p.h.pause()
            p.pump(0.1)
        self.assertNotIn("SW-502", p.uh.problems())
        self.assertFalse(p.pf.playing)

    def test_skip_spam_sends_one_jump(self):
        p = self.pair
        p.connect()
        p.h.play()
        p.pump(0.4)
        seeks_before = p.ph.seeks
        for _ in range(6):
            p.f.skip(10_000)
            p.pump(0.05)
        p.pump(1.0)
        self.assertEqual(p.ph.seeks - seeks_before, 1)   # the host jumped once, not six times
        self.assertLess(abs(p.gap()), 150)

    def test_no_automatic_correction_and_sync_me(self):
        p = self.pair
        p.connect()
        p.h.play()
        p.pump(0.5)
        seeks = p.pf.seeks
        p.pf.drift(3000)                  # friend silently 3 s ahead
        p.pump(3.5)
        self.assertEqual(p.pf.seeks, seeks)  # nothing jumped by itself
        self.assertAlmostEqual(p.f.gap_ms(), 3000, delta=150)
        self.assertAlmostEqual(p.h.gap_ms(), -3000, delta=150)
        p.f.sync_me()
        p.pump(0.3)
        self.assertLess(abs(p.gap()), 120)
        self.assertTrue(p.ph.playing and p.pf.playing)

    def test_sync_up_waits_for_slow_friend(self):
        p = Pair(friend_seek_delay=1.2)
        try:
            p.connect()
            p.h.play()
            p.pump(0.4)
            p.pf.drift(-2500)
            p.h.sync_up()
            p.pump(0.6)
            self.assertFalse(p.ph.playing)   # still waiting for the slow friend
            p.pump(2.5)                      # friend ready after 1.2 s, then 1 s countdown
            self.assertTrue(p.ph.playing and p.pf.playing)
            self.assertLess(abs(p.gap()), 80)
            self.assertNotIn("SW-503", p.uh.problems())
            self.assertTrue(p.uh.countdowns and p.uf.countdowns)
        finally:
            p.close()

    def test_sync_up_does_not_wait_forever(self):
        p = Pair(host_settings=make_settings(sync__ready_timeout_s=1.0), friend_seek_delay=10.0)
        try:
            p.connect()
            p.pump(0.3)
            p.h.play()
            p.pump(0.3)
            p.h.sync_up()
            p.pump(2.5)
            self.assertTrue(p.ph.playing)
            self.assertIn("SW-503", p.uh.problems())
        finally:
            p.close()

    def test_friend_sync_up_request_and_cooldown(self):
        p = self.pair
        p.connect()
        p.h.play()
        p.pump(0.3)
        p.f.sync_up()
        p.pump(2.0)
        self.assertTrue(p.ph.playing and p.pf.playing)
        p.f.sync_up()                     # 15 s cooldown
        p.pump(0.3)
        self.assertIn("SW-502", p.uf.problems())

    def test_host_only_mode(self):
        p = Pair(host_settings=make_settings(sync__host_only=True))
        try:
            p.connect()
            p.h.play()
            p.pump(1.5)                   # friend learns the rules from the host's heartbeat
            p.f.pause()                   # works only for the friend
            p.pump(0.5)
            self.assertTrue(p.ph.playing)
            self.assertFalse(p.pf.playing)
            self.assertFalse(p.f.attached)
            p.h.pause()                   # friend is on their own: ignores the room
            p.pump(0.3)
            p.h.play()
            p.pump(0.3)
            p.f.sync_up()                 # not allowed in host-only mode
            p.pump(0.2)
            self.assertIn("SW-504", p.uf.problems())
            p.f.sync_me()                 # rejoin
            p.pump(0.3)
            self.assertTrue(p.f.attached and p.pf.playing)
            self.assertLess(abs(p.gap()), 120)
        finally:
            p.close()

    def test_film_mismatch_warning(self):
        p = self.pair
        p.h.set_film("Dune.mkv", 1000, 9_000_000)
        p.f.set_film("Dune.other.mkv", 2000, 9_300_000)
        p.connect()
        p.pump(0.3)
        self.assertIn("SW-501", p.uf.problems())
        self.assertIn("SW-501", p.uh.problems())

    def test_similar_film_is_fine(self):
        p = self.pair
        p.h.set_film("Dune.mkv", 1000, 9_000_000)
        p.f.set_film("Dune.other.mkv", 2000, 9_000_400)
        p.connect()
        p.pump(0.3)
        self.assertNotIn("SW-501", p.uf.problems())

    def test_graceful_leave(self):
        p = self.pair
        p.connect()
        p.f.leave()
        p.pump(0.5)
        self.assertFalse(p.h.connected)
        self.assertNotIn("SW-307", p.uh.problems())


class SafetyTests(unittest.TestCase):
    """Things a broken or hostile app might send to the host."""

    def setUp(self):
        self.q = queue.Queue()
        self.ui = RecorderUI()
        self.player = FakePlayer()
        self.host = Controller(self.player, self.ui, make_settings(), self.q.put)
        self.host.set_profile("Alex", None)
        self.assertTrue(self.host.host())

    def tearDown(self):
        self.host.leave()

    def pump(self, seconds):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            while not self.q.empty():
                self.host.handle(self.q.get())
            self.host.tick()
            time.sleep(0.01)

    def raw(self):
        sock = socket.create_connection(("127.0.0.1", PORT), timeout=3)
        sock.recv(65536)  # the host's hello
        return sock

    def hello(self, sock, version=PROTOCOL_VERSION):
        sock.sendall((json.dumps({"t": "hello", "v": version, "app": "x", "name": "Eve"}) + "\n").encode())

    def test_garbage(self):
        sock = self.raw()
        sock.sendall(b"this is not json\n")
        self.pump(0.5)
        self.assertIn("SW-309", self.ui.problems())
        sock.settimeout(3)
        while sock.recv(4096):  # anything already sent (e.g. a ping) may arrive first...
            pass                # ...but then the connection must be closed (recv returns b"")
        sock.close()

    def test_message_before_hello(self):
        sock = self.raw()
        sock.sendall(b'{"t":"req","action":"pause"}\n')
        self.pump(0.5)
        self.assertIn("SW-309", self.ui.problems())
        sock.close()

    def test_wrong_types_and_huge_numbers(self):
        sock = self.raw()
        self.hello(sock)
        sock.sendall(b'{"t":"state","pos":1e300,"playing":true}\n')
        self.pump(0.5)
        self.assertIn("SW-309", self.ui.problems())
        sock.close()

    def test_too_long_line(self):
        sock = self.raw()
        self.hello(sock)
        try:
            sock.sendall(b'{"t":"profile","name":"' + b"A" * 300_000 + b'"}\n')
        except OSError:
            pass
        self.pump(0.5)
        self.assertIn("SW-309", self.ui.problems())
        sock.close()

    def test_flood(self):
        sock = self.raw()
        self.hello(sock)
        try:
            for _ in range(400):
                sock.sendall(b'{"t":"ping","id":1}\n')
        except OSError:
            pass
        self.pump(0.5)
        self.assertIn("SW-309", self.ui.problems())
        sock.close()

    def test_unknown_message_types_are_ignored(self):
        sock = self.raw()
        self.hello(sock)
        sock.sendall(b'{"t":"from_the_future","x":1}\n{"t":"ping","id":5}\n')
        self.pump(0.5)
        self.assertNotIn("SW-309", self.ui.problems())
        self.assertTrue(self.host.connected)
        sock.close()

    def test_bad_avatar_is_ignored(self):
        sock = self.raw()
        sock.sendall((json.dumps({"t": "hello", "v": PROTOCOL_VERSION, "app": "x", "name": "Eve",
                                  "avatar": "bm90IGFuIGltYWdl"}) + "\n").encode())
        self.pump(0.5)
        self.assertIn("SW-803", self.ui.problems())
        self.assertIsNone(self.host.peer.avatar)
        sock.close()

    def test_version_mismatch(self):
        sock = self.raw()
        self.hello(sock, version=PROTOCOL_VERSION + 1)
        self.pump(0.5)
        self.assertIn("SW-308", self.ui.problems())
        sock.close()

    def test_second_friend_is_refused(self):
        first = self.raw()
        self.hello(first)
        self.pump(0.3)
        q2, ui2 = queue.Queue(), RecorderUI()
        other = Controller(FakePlayer(), ui2, make_settings(), q2.put)
        other.join("127.0.0.1")
        end = time.monotonic() + 1.0
        while time.monotonic() < end:
            while not q2.empty():
                other.handle(q2.get())
            self.pump(0.05)
        self.assertIn("SW-310", ui2.problems())
        self.assertTrue(self.host.connected)  # the first friend wasn't kicked out
        first.close()

    def test_silent_friend_is_noticed(self):
        old = link_module.DEAD_AFTER_S
        link_module.DEAD_AFTER_S = 1.0
        try:
            sock = self.raw()
            self.hello(sock)
            self.pump(3.5)
            self.assertIn("SW-307", self.ui.problems())
            self.assertFalse(self.host.connected)
            sock.close()
        finally:
            link_module.DEAD_AFTER_S = old


class ConnectErrorTests(unittest.TestCase):
    def run_join(self, address, port=PORT):
        q, ui = queue.Queue(), RecorderUI()
        controller = Controller(FakePlayer(), ui, make_settings(sync__port=port), q.put)
        controller.join(address)
        end = time.monotonic() + 10
        while time.monotonic() < end and not ui.problems():
            while not q.empty():
                controller.handle(q.get())
            time.sleep(0.02)
        controller.leave()
        return ui.problems()

    def test_refused(self):
        self.assertEqual(self.run_join("127.0.0.1", port=50612), ["SW-305"])

    def test_bad_address(self):
        self.assertEqual(self.run_join("not an address"), ["SW-303"])
        self.assertEqual(self.run_join("1.2.3.4:99999"), ["SW-303"])

    def test_unknown_name(self):
        self.assertIn(self.run_join("no-such-host.invalid")[0], ("SW-306", "SW-304", "SW-312"))

    def test_port_in_use(self):
        blocker = socket.socket()
        blocker.bind(("0.0.0.0", 50613))
        blocker.listen(1)
        try:
            q, ui = queue.Queue(), RecorderUI()
            controller = Controller(FakePlayer(), ui, make_settings(sync__port=50613), q.put)
            self.assertFalse(controller.host())
            while not q.empty():
                controller.handle(q.get())
            self.assertEqual(ui.problems(), ["SW-301"])
        finally:
            blocker.close()

    def test_address_parsing(self):
        parse = link_module.parse_address
        self.assertEqual(parse("100.64.1.2", 50505), ("100.64.1.2", 50505))
        self.assertEqual(parse(" 192.168.1.5:6000 ", 50505), ("192.168.1.5", 6000))
        self.assertEqual(parse("[fd7a::1]:7000", 50505), ("fd7a::1", 7000))
        self.assertEqual(parse("my-pc", 50505), ("my-pc", 50505))
        for bad in ("", "a b", "1.2.3", "host:abc", "-bad", "x" * 300):
            with self.assertRaises(ValueError, msg=bad):
                parse(bad, 50505)


if __name__ == "__main__":
    unittest.main()
