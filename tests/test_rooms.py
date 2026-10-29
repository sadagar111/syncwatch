"""Rooms end to end: joining, approval, keys, watching in sync with several people, friends.

Every person is a real Controller with its own identity and relay connection
(tests/room_harness.py), talking through tools/local_relay.py.
"""
import base64
import os
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from room_harness import Room  # noqa: E402

from syncwatch import PROTOCOL_VERSION  # noqa: E402
from syncwatch.net import relay as relay_module  # noqa: E402
from syncwatch.net.relay import Relay  # noqa: E402
from syncwatch.net.websocket import WebSocket  # noqa: E402
from syncwatch.room import crypto, session as session_module  # noqa: E402
from syncwatch.room.codes import parse  # noqa: E402
from syncwatch.room.identity import Identity  # noqa: E402
from syncwatch.sync.controller import PAUSE_ALIGN_MS  # noqa: E402

FAST = {"MEMBER_TIMEOUT_S": 3.0, "HOST_LOST_S": 2.0, "NOBODY_AFTER_S": 1.0, "HELLO_EVERY_S": 0.5,
        "HELLO_WHILE_ASKING_S": 1.0, "MEMBERS_EVERY_S": 2.0}


class RoomTestCase(unittest.TestCase):
    def setUp(self):
        self._saved = {name: getattr(session_module, name) for name in FAST}
        for name, value in FAST.items():
            setattr(session_module, name, value)
        self._backoff = relay_module.BACKOFF_S
        relay_module.BACKOFF_S = (0.1, 0.2, 0.4)
        self.room = Room()

    def tearDown(self):
        self.room.close()
        for name, value in self._saved.items():
            setattr(session_module, name, value)
        relay_module.BACKOFF_S = self._backoff

    def pump(self, seconds=0.0, until=None, timeout=15.0):
        return self.room.pump(seconds, until, timeout)


class JoiningTests(RoomTestCase):
    def test_ask_approve_and_see_each_other(self):
        alex, marek = self.room.person("Alex"), self.room.person("Marek")
        self.assertTrue(alex.c.host_room())
        self.assertTrue(marek.c.join_room(alex.code()))
        self.assertTrue(self.pump(until=lambda: alex.c.session.waiting))
        knock = next(iter(alex.c.session.waiting.values()))
        self.assertEqual(knock.name, "Marek")
        self.assertEqual(knock.key, marek.rooms.identity.public)   # the host sees the real ID
        self.assertFalse(marek.c.connected)                          # still waiting for the OK
        self.assertTrue(alex.ui.requests)
        alex.c.approve(knock.id)
        self.assertTrue(self.pump(until=lambda: marek.c.connected and alex.peer(marek) is not None))
        self.assertTrue(self.pump(until=lambda: marek.peer(alex) is not None and marek.peer(alex).known))
        self.assertTrue(marek.peer(alex).is_host)
        self.assertIn("Marek joined", alex.ui.activities())
        self.assertIn("You're in Alex's room", marek.ui.activities())

    def test_deny_and_block(self):
        alex, eve = self.room.person("Alex"), self.room.person("Eve")
        alex.c.host_room()
        eve.c.join_room(alex.code())
        self.assertTrue(self.pump(until=lambda: alex.c.session.waiting))
        alex.c.deny(eve.id, block=True)
        self.assertTrue(self.pump(until=lambda: "SW-409" in eve.ui.problems()))
        self.assertFalse(eve.c.connected)
        self.assertTrue(alex.rooms.store.is_blocked(eve.rooms.identity.public))
        eve.c.join_room(alex.code())  # blocked: turned away without asking the host
        self.assertTrue(self.pump(until=lambda: eve.ui.problems().count("SW-409") == 2))
        self.assertFalse(alex.c.session.waiting)

    def test_policies(self):
        alex = self.room.person("Alex", room__policy="friends_only")
        friend, stranger = self.room.person("Friend"), self.room.person("Stranger")
        alex.rooms.store.add_friend(friend.rooms.identity.public, "Friend", friend.c.friends.mailbox_id)
        alex.c.host_room()
        stranger.c.join_room(alex.code())
        self.assertTrue(self.pump(until=lambda: "SW-416" in stranger.ui.problems()))
        friend.c.join_room(alex.code())  # friends may ask (and still need the OK)
        self.assertTrue(self.pump(until=lambda: alex.c.session.waiting))
        alex.settings.set("room.policy", "friends_direct")
        friend.c.leave()
        friend.c.join_room(alex.code())
        self.assertTrue(self.pump(until=lambda: friend.c.connected))
        alex.settings.set("room.policy", "open")
        stranger.c.join_room(alex.code())
        self.assertTrue(self.pump(until=lambda: stranger.c.connected))

    def test_room_size_limit(self):
        alex = self.room.person("Alex", room__max_people=2)
        a, b = self.room.person("A"), self.room.person("B")
        self.room.let_in(alex, a)
        b.c.join_room(alex.code())
        self.assertTrue(self.pump(until=lambda: "SW-410" in b.ui.problems()))

    def test_nobody_hosting_then_host_arrives(self):
        alex, marek = self.room.person("Alex"), self.room.person("Marek")
        code = alex.rooms.store.my_code(alex.rooms.identity, alex.room.relay_url).text()
        marek.c.join_room(code)
        self.assertTrue(self.pump(until=lambda: "SW-415" in marek.ui.problems()))
        alex.c.host_room()
        self.assertTrue(self.pump(until=lambda: alex.c.session.waiting))
        alex.c.approve_all()
        self.assertTrue(self.pump(until=lambda: marek.c.connected))

    def test_bad_codes(self):
        marek = self.room.person("Marek")
        self.assertFalse(marek.c.join_room("SW-1234"))
        self.assertFalse(marek.c.join_room("hello"))
        self.assertEqual(marek.ui.problems(), ["SW-407", "SW-407"])

    def test_different_versions(self):
        alex, old = self.room.person("Alex"), self.room.person("Old")
        alex.c.host_room()
        self.pump(until=lambda: alex.c.session.channel_state == "connected")
        old.c.session.protocol = PROTOCOL_VERSION + 1     # "Old" speaks another protocol version
        old.c.join_room(alex.code())
        self.assertTrue(self.pump(until=lambda: "SW-413" in old.ui.problems()))
        self.assertFalse(old.c.connected)


class WatchingTests(RoomTestCase):
    def setUp(self):
        super().setUp()
        self.alex = self.room.person("Alex")
        self.guests = [self.room.person(n, clock_offset=off) for n, off in (("Marek", 1234.5), ("Kuba", -77.0))]
        self.room.let_in(self.alex, *self.guests)
        self.pump(until=lambda: all(g.c.session.clock_ready for g in self.guests))

    def everyone(self):
        return [self.alex] + self.guests

    def test_clock_sync_despite_different_pc_clocks(self):
        for guest in self.guests:
            difference = guest.c.session.host_clock_ms() - self.alex.c.session.host_clock_ms()
            self.assertLess(abs(difference), 50, guest.name)

    def test_host_play_pause_seek_reach_everyone(self):
        self.alex.c.play()
        self.assertTrue(self.pump(until=lambda: all(g.player.playing for g in self.guests)))
        self.pump(1.0)
        for guest in self.guests:
            self.assertLess(abs(guest.player.position() - self.alex.player.position()), 150, guest.name)
        self.alex.c.seek(600_000)
        self.assertTrue(self.pump(until=lambda: all(abs(g.player.position() - 600_000) < 2000
                                                    for g in self.guests)))
        self.alex.c.pause()
        self.assertTrue(self.pump(until=lambda: not any(g.player.playing for g in self.guests)))
        for guest in self.guests:
            self.assertLess(abs(guest.player.position() - self.alex.player.position()), PAUSE_ALIGN_MS)

    def test_members_request_and_host_decides(self):
        marek, kuba = self.guests
        self.alex.settings.set("sync.cooldown_playpause_s", 0.5)
        self.assertTrue(self.pump(until=lambda: marek.c.rules.cd_pp == 0.5))
        marek.c.play()                         # a request: nothing happens until the host agrees
        self.assertTrue(self.pump(until=lambda: all(p.player.playing for p in self.everyone())))
        self.assertTrue(any("You pressed play" in a for a in marek.ui.activities()))
        self.assertTrue(any("Marek pressed play" in a for a in kuba.ui.activities()))
        self.pump(0.6)
        marek.c.pause()
        marek.c.pause()                        # too soon: cooldown on Marek's side
        self.assertIn("SW-502", marek.ui.problems())
        self.assertTrue(self.pump(until=lambda: not self.alex.player.playing))
        kuba.c.seek(300_000)
        self.assertTrue(self.pump(until=lambda: all(abs(p.player.position() - 300_000) < 2000
                                                    for p in self.everyone())))

    def test_host_only_mode(self):
        marek = self.guests[0]
        self.alex.settings.set("sync.host_only", True)
        self.assertTrue(self.pump(until=lambda: marek.c.rules.host_only))
        marek.c.play()                          # works just for Marek now
        self.assertFalse(marek.c.attached)
        self.pump(0.5)
        self.assertFalse(self.alex.player.playing)
        self.alex.c.play()
        self.assertTrue(self.pump(until=lambda: self.guests[1].player.playing))
        marek.c.sync_up()
        self.assertIn("SW-504", marek.ui.problems())
        marek.c.sync_me()
        self.assertTrue(marek.c.attached)

    def test_sync_up_waits_for_everyone(self):
        slow = self.room.person("Slow", seek_delay=1.0)
        self.room.let_in(self.alex, slow)
        self.alex.c.play()
        self.assertTrue(self.pump(until=lambda: slow.player.playing))
        for guest in self.guests:
            guest.player.drift(-3000)
        self.guests[0].c.sync_up()
        self.assertTrue(self.pump(until=lambda: self.alex.ui.countdowns))
        self.assertTrue(self.pump(until=lambda: all(p.player.playing for p in self.room.people)))
        self.pump(0.5)
        positions = [p.player.position() for p in self.room.people]
        self.assertLess(max(positions) - min(positions), 250)

    def test_sync_up_timeout_names_the_slow_one(self):
        self.alex.settings.set("sync.ready_timeout_s", 1.0)
        stuck = self.guests[1]
        stuck.player.seek_delay = 30.0
        self.alex.c.sync_up()
        self.assertTrue(self.pump(until=lambda: "SW-503" in self.alex.ui.problems()))

    def test_gap_and_sync_me(self):
        marek = self.guests[0]
        self.alex.c.play()
        self.assertTrue(self.pump(until=lambda: marek.player.playing))
        marek.player.drift(-2500)
        self.assertTrue(self.pump(until=lambda: marek.c.gap_ms() is not None and marek.c.gap_ms() < -2000))
        self.pump(2.0)
        self.assertLess(marek.player.position(), self.alex.player.position() - 2000)  # no automatic jumping
        marek.c.sync_me()
        self.assertLess(abs(marek.player.position() - self.alex.player.position()), 250)
        peer = self.alex.peer(marek)
        self.assertTrue(self.pump(until=lambda: abs(self.alex.c.peer_gap_ms(peer) or 9999) < 500))

    def test_films_and_profiles(self):
        marek = self.guests[0]
        self.alex.c.set_film("film.mkv", 1000, 7_200_000)
        marek.c.set_film("other.mkv", 2000, 5_000_000)
        self.assertTrue(self.pump(until=lambda: "SW-501" in marek.ui.problems()
                                  and "SW-501" in self.alex.ui.problems()))
        png = b"\x89PNG\r\n\x1a\n" + os.urandom(2000)
        marek.c.set_profile("Marek K", png)
        self.assertTrue(self.pump(until=lambda: self.alex.peer(marek).avatar == png
                                  and self.guests[1].peer(marek).name == "Marek K"))


class SecurityTests(RoomTestCase):
    def setUp(self):
        super().setUp()
        self.alex, self.marek = self.room.person("Alex"), self.room.person("Marek")
        self.room.let_in(self.alex, self.marek)
        self.code = parse(self.alex.code())
        self.keys = crypto.room_keys(self.code)
        relay = Relay(self.room.relay_url)
        self.spy = WebSocket.connect(relay.channel_url(self.keys.channel_id))
        self.pump(0.3)

    def tearDown(self):
        self.spy.close()
        super().tearDown()

    def recorded(self, seconds=1.5):
        got, end = [], time.monotonic() + seconds
        while time.monotonic() < end:
            try:
                got.append(self.spy.recv(timeout=0.05))
            except OSError:
                pass
            self.pump()
        return got

    def test_a_leaked_code_doesnt_open_the_room(self):
        self.alex.c.play()
        messages = self.recorded()
        rooms = [m for m in messages if crypto.peek(m)[0] == crypto.ROOM]
        self.assertTrue(rooms)
        for data in rooms:  # the code gives the lobby key, but the room key was never shared with us
            with self.assertRaises(crypto.BadEnvelope):
                crypto.open_room(data, self.keys.channel, {})
            with self.assertRaises(crypto.BadEnvelope):
                crypto.open_lobby(data, self.keys.channel, self.keys.lobby_key)

    def test_a_fake_host_is_ignored(self):
        eve = Identity.generate()
        card = {"t": "card", "s": "0" * 16, "n": 1, "name": "Alex", "dh": base64.b64encode(eve.public.dh_pub).decode(),
                "app": "0.4.0", "v": PROTOCOL_VERSION, "challenge": base64.b64encode(os.urandom(16)).decode(),
                "people": 1, "max": 10, "policy": "open"}
        newcomer = self.room.person("Newcomer")
        newcomer.c.join_room(self.alex.code())
        self.pump(until=lambda: newcomer.c.session.channel_state == "connected")
        self.spy.send(crypto.pack_lobby(self.keys.channel, self.keys.lobby_key, eve, card))
        self.assertTrue(self.pump(until=lambda: "SW-408" in newcomer.ui.problems()))
        self.assertTrue(self.pump(until=lambda: self.alex.c.session.waiting))   # still asks the real host

    def test_replayed_and_forged_commands_are_ignored(self):
        self.alex.c.play()
        self.assertTrue(self.pump(until=lambda: self.marek.player.playing))
        self.alex.c.pause()
        recorded = self.recorded()
        self.assertFalse(self.marek.player.playing)
        self.alex.c.play()
        self.assertTrue(self.pump(until=lambda: self.marek.player.playing))
        for data in recorded:   # re-send everything we saw (incl. the host's "pause")
            self.spy.send(data)
        self.pump(1.0)
        self.assertTrue(self.marek.player.playing)
        # A member can't send host commands either.
        kuba = self.room.person("Kuba")
        self.room.let_in(self.alex, kuba)
        session = kuba.c.session
        session.send_room("cmd", action="pause", pos=0, playing=False, at=session.host_clock_ms())
        self.pump(1.0)
        self.assertTrue(self.marek.player.playing)

    def test_kicked_people_cant_read_on(self):
        kuba = self.room.person("Kuba")
        self.room.let_in(self.alex, kuba)
        old_keys = dict(kuba.c.session.epochs)
        self.alex.c.kick(kuba.id, ban=True)
        self.assertTrue(self.pump(until=lambda: "SW-411" in kuba.ui.problems()))
        self.assertTrue(self.pump(until=lambda: kuba.id not in self.marek.c.peers))
        self.recorded(1.0)       # (skip what was sent before the kick: the spy hasn't read it yet)
        self.alex.c.play()
        later = [m for m in self.recorded() if crypto.peek(m)[0] == crypto.ROOM]
        self.assertTrue(later)
        for data in later:
            with self.assertRaises(crypto.BadEnvelope):
                crypto.open_room(data, self.keys.channel, old_keys)
        self.assertTrue(self.alex.rooms.store.is_blocked(kuba.rooms.identity.public))


class ResilienceTests(RoomTestCase):
    def test_host_closes_and_member_leaves(self):
        alex, marek, kuba = self.room.person("Alex"), self.room.person("Marek"), self.room.person("Kuba")
        self.room.let_in(alex, marek, kuba)
        kuba.c.leave()
        self.assertTrue(self.pump(until=lambda: kuba.id not in alex.c.peers and kuba.id not in marek.c.peers))
        alex.c.leave()
        self.assertTrue(self.pump(until=lambda: "SW-412" in marek.ui.problems()))
        self.assertFalse(marek.c.connected)

    def test_lost_connection_gets_back_in_without_asking(self):
        alex, marek = self.room.person("Alex"), self.room.person("Marek")
        self.room.let_in(alex, marek)
        marek.c.session.channel.reconnect_now()
        self.room.people.remove(marek)            # Marek's PC freezes for a while
        self.assertTrue(self.pump(until=lambda: marek.id not in alex.c.peers, timeout=10))
        self.room.people.append(marek)
        self.assertTrue(self.pump(until=lambda: marek.c.connected and marek.id in alex.c.peers, timeout=20))
        self.assertFalse(alex.c.session.waiting)   # no approval needed again
        self.assertIn("Back in Alex's room", marek.ui.activities())

    def test_host_restarts_and_everyone_returns(self):
        alex, marek = self.room.person("Alex"), self.room.person("Marek")
        self.room.let_in(alex, marek)
        alex.c.session._close_channel()          # crash: no goodbye
        alex.c.session._reset()
        self.assertTrue(self.pump(until=lambda: any("Lost contact" in a for a in marek.ui.activities())))
        alex.c.host_room()                       # SyncWatch started again: same code, new keys
        self.assertTrue(self.pump(until=lambda: marek.c.connected and marek.id in alex.c.peers, timeout=20))
        self.assertFalse(alex.c.session.waiting)

    def test_new_code_moves_everyone(self):
        alex, marek, late = self.room.person("Alex"), self.room.person("Marek"), self.room.person("Late")
        self.room.let_in(alex, marek)
        old = alex.code()
        alex.c.new_code()
        self.assertNotEqual(alex.code(), old)
        self.assertTrue(self.pump(until=lambda: marek.c.connected and marek.id in alex.c.peers
                                  and parse(marek.c.session.code.text()) == parse(alex.code()), timeout=20))
        late.c.join_room(old)                    # the old code leads nowhere now
        self.assertTrue(self.pump(until=lambda: "SW-415" in late.ui.problems()))

    def test_relay_restart(self):
        if not hasattr(self.room.relay, "drop_all"):
            self.skipTest("restarting the relay needs the one on this PC (tools/local_relay.py)")
        alex, marek = self.room.person("Alex"), self.room.person("Marek")
        self.room.let_in(alex, marek)
        alex.c.play()
        self.assertTrue(self.pump(until=lambda: marek.player.playing))
        self.room.relay.drop_all()
        self.pump(1.0)
        alex.c.pause()
        self.assertTrue(self.pump(until=lambda: not marek.player.playing, timeout=10))

    def test_ten_people(self):
        host = self.room.person("Host")
        guests = [self.room.person(f"Guest{i}") for i in range(9)]
        self.room.let_in(host, *guests, timeout=30)
        self.assertEqual(len(host.c.peers), 9)
        self.assertTrue(self.pump(until=lambda: all(len(g.c.peers) == 9 for g in guests), timeout=20))
        guests[4].c.play()
        self.assertTrue(self.pump(until=lambda: all(p.player.playing for p in self.room.people), timeout=10))
        host.c.sync_up()
        self.assertTrue(self.pump(until=lambda: host.ui.countdowns, timeout=10))
        self.assertTrue(self.pump(until=lambda: all(p.player.playing for p in self.room.people), timeout=10))
        self.pump(0.5)
        positions = [p.player.position() for p in self.room.people]
        self.assertLess(max(positions) - min(positions), 300)


class FriendTests(RoomTestCase):
    def test_become_friends_and_join_from_the_list(self):
        alex, marek = self.room.person("Alex"), self.room.person("Marek")
        alex.c.start()
        marek.c.start()
        self.room.let_in(alex, marek)
        self.assertTrue(alex.c.friends.request(alex.c.session, alex.c.session.member(marek.id)))
        self.assertTrue(self.pump(until=lambda: marek.id and alex.id in marek.c.friends.incoming))
        marek.c.friends.accept(marek.c.session, alex.id)
        self.assertTrue(self.pump(until=lambda: marek.id in alex.rooms.store.friends))
        self.assertIn(alex.id, marek.rooms.store.friends)
        marek.c.leave()
        alex.c.leave()
        self.pump(0.5)
        alex.c.host_room()
        self.assertTrue(self.pump(until=lambda: alex.id in marek.c.friends.rooms))
        room = marek.c.friends.room_of(alex.id)
        marek.c.join_code(room.code)                     # one click, no code to paste
        self.assertTrue(self.pump(until=lambda: alex.c.session.waiting))   # still asks (ask every time)
        alex.c.approve_all()
        self.assertTrue(self.pump(until=lambda: marek.c.connected))
        alex.c.leave()
        self.assertTrue(self.pump(until=lambda: alex.id not in marek.c.friends.rooms))

    def test_rooms_already_open_show_up_and_unfriending(self):
        alex, marek = self.room.person("Alex"), self.room.person("Marek")
        alex.rooms.store.add_friend(marek.rooms.identity.public, "Marek", marek.c.friends.mailbox_id)
        marek.rooms.store.add_friend(alex.rooms.identity.public, "Alex", alex.c.friends.mailbox_id)
        alex.c.start()
        alex.c.host_room()
        self.pump(1.0)
        marek.c.start()                                  # opens SyncWatch later: asks who's hosting
        self.assertTrue(self.pump(until=lambda: alex.id in marek.c.friends.rooms))
        marek.c.friends.remove(alex.id)
        self.assertTrue(self.pump(until=lambda: marek.id not in alex.rooms.store.friends))

    def test_strangers_cant_use_your_mailbox(self):
        marek = self.room.person("Marek")
        marek.c.start()
        self.pump(0.5)
        eve = Identity.generate()
        relay = Relay(self.room.relay_url)
        fake = parse(self.room.person("Alex").rooms.store.my_code(eve, self.room.relay_url).text())
        body = {"t": "hosting", "s": "0" * 16, "n": 1, "code": fake.text(), "room": "Free films", "ts": time.time()}
        relay.post(marek.c.friends.mailbox_id,
                   crypto.pack_direct(marek.c.friends.mailbox_bytes, marek.rooms.identity.public, eve, body))
        self.pump(1.0)
        self.assertFalse(marek.c.friends.rooms)


if __name__ == "__main__":
    unittest.main()
