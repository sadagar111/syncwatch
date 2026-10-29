"""Hostile and random input for everything that reads data from the network.

What a stranger with a room code, someone in the room with a modified app, or a
broken relay could send. Nothing may raise anything but the expected errors
(BadEnvelope, BadMessage, CodeError, the WebSocket errors), and the room must
keep working afterwards.

Seeded, so a failure can be repeated: the message names the seed. For a longer
hunt: SYNCWATCH_FUZZ_ROUNDS=20 python -m unittest tests.test_fuzz
"""
import base64
import json
import os
import random
import socket
import sys
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305  # noqa: E402

from room_harness import Room  # noqa: E402

from syncwatch.net import protocol  # noqa: E402
from syncwatch.net.protocol import B64, BadMessage, Hex, ListOf  # noqa: E402
from syncwatch.net.websocket import WebSocket, WebSocketError  # noqa: E402
from syncwatch.room import crypto, session as session_module  # noqa: E402
from syncwatch.room.codes import CodeError, RoomCode, parse  # noqa: E402
from syncwatch.room.identity import Identity, PublicIdentity  # noqa: E402

SEED = int(os.environ.get("SYNCWATCH_FUZZ_SEED", "20261029"))
ROUNDS = max(1, int(os.environ.get("SYNCWATCH_FUZZ_ROUNDS", "1")))

# Values a modified app might put anywhere.
NASTY = [None, True, False, 0, -1, 1, 7, 2 ** 31, -2 ** 63, 2 ** 64, 10 ** 30, 0.5, -0.0, 1e308, -1e308,
         "", " ", "x" * 5000, "\x00\x01\x7f\x1b[31m", "‮evil⁦", "\U0001F3AC" * 40, "\ud800", "a\udfffb",
         "AAAA", "====", "0" * 32, "g" * 32, "SW-", "SW-" + "0" * 44,
         [], [1, 2], [[[[]]]], ["a"] * 100, {}, {"t": "x"}, {"a": {"b": {"c": {}}}}]
# Low-order X25519 keys: nobody can encrypt to them (sending to one fails).
BAD_DH = [bytes(32), b"\x01" + bytes(31),
          bytes.fromhex("ecffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff7f")]
# Message bodies that aren't sensible JSON objects, as raw bytes.
HOSTILE_BODIES = [b"[" * 30000 + b"]" * 30000, b'{"t":' * 20000 + b'"x"' + b"}" * 20000,
                  b'{"t":"hello","a":' + b"[" * 5000 + b"]" * 5000 + b"}", b"1" * 5000, b'{"t":"x","n":1e999}',
                  b'{"t":"x","n":NaN}', b'{"t":"x","n":Infinity}', b"\xff\xfe\x00", b"\xef\xbb\xbf{}", b"",
                  b"null", b"[]", b'"t"', b'{"t":1}', b'{"t":null}', b'{"t":"x","t":"y"}', b"{" * 3,
                  b'{"t":"\\ud800"}']


def example(spec_entry, rng=None):
    """A valid value for one field of a protocol table."""
    kind, _required, limit = spec_entry
    if isinstance(kind, dict):
        return {name: example(entry) for name, entry in kind.items() if entry[1]}
    if isinstance(kind, ListOf):
        return [example((kind.item, True, None))]
    if isinstance(kind, B64):
        return base64.b64encode(bytes(kind.size)).decode("ascii")
    if isinstance(kind, Hex):
        return "0" * kind.chars
    if isinstance(kind, tuple):
        return kind[0]
    if kind is bool:
        return True
    if kind in (int, float):
        return kind(limit[0])
    return "x"


def valid_body(table, kind, rng):
    body = {"t": kind, "s": "%016x" % rng.getrandbits(64), "n": rng.randrange(1, 1 << 40)}
    for name, entry in table[kind].items():
        if entry[1] or rng.random() < 0.5:
            body[name] = example(entry)
    return body


def mutate(body, table, rng):
    """A copy of a valid body with a few fields broken, dropped or added."""
    body = dict(body)
    names = [n for n in body if n != "t"] or ["x"]
    for _ in range(rng.randint(1, 3)):
        roll = rng.random()
        name = rng.choice(names)
        if roll < 0.55:
            body[name] = rng.choice(NASTY)
        elif roll < 0.7:
            body.pop(name, None)
        elif roll < 0.8:
            body["extra" + str(rng.randint(0, 9))] = rng.choice(NASTY)
        elif roll < 0.9 and isinstance(body.get(name), list) and body[name] and isinstance(body[name][0], dict):
            item = dict(body[name][0])
            item[rng.choice(list(item) or ["x"])] = rng.choice(NASTY)
            body[name] = [item] * rng.randint(1, 70)
        else:
            body["t"] = rng.choice(list(table) + ["", "x" * 300, "knock", "cmd", "card", "zzz"])
    return body


def conforms(value, entry) -> bool:
    """Whether a value that passed validation really has the shape the table promises."""
    kind, _required, limit = entry
    if isinstance(kind, dict):
        return isinstance(value, dict) and set(value) <= set(kind) and all(
            conforms(v, kind[k]) for k, v in value.items())
    if isinstance(kind, ListOf):
        return isinstance(value, list) and len(value) <= kind.max_items and all(
            conforms(v, (kind.item, True, None)) for v in value)
    if isinstance(kind, B64):
        return isinstance(value, bytes) and len(value) == kind.size
    if isinstance(kind, Hex):
        return isinstance(value, str) and len(value) == kind.chars
    if isinstance(kind, tuple):
        return value in kind
    if kind is bool:
        return isinstance(value, bool)
    if kind is int:
        return isinstance(value, int) and not isinstance(value, bool) and limit[0] <= value <= limit[1]
    if kind is float:
        return isinstance(value, float) and limit[0] <= value <= limit[1]
    if kind is str:
        return (isinstance(value, str) and len(value) <= limit
                and not any(ch < " " and ch != "\t" for ch in value)
                and not any(0xD800 <= ord(ch) <= 0xDFFF for ch in value)
                and not any(ch in protocol.INVISIBLE for ch in value))
    return False


def raw_envelope(kind, key, channel, sender, body_bytes, key_field=None, recipient=None):
    """An envelope around any bytes, correctly encrypted and signed (what crypto.pack_* won't make)."""
    if kind == crypto.DIRECT:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
        ephemeral = X25519PrivateKey.generate()
        ephemeral_pub = ephemeral.public_key().public_bytes(serialization.Encoding.Raw,
                                                            serialization.PublicFormat.Raw)
        shared = ephemeral.exchange(X25519PublicKey.from_public_bytes(recipient.dh_pub))
        key = crypto._direct_key(shared, ephemeral_pub, recipient.dh_pub, channel)
        header = bytes([crypto.VERSION, kind]) + recipient.key_id + ephemeral_pub + os.urandom(crypto.NONCE)
    else:
        header = bytes([crypto.VERSION, kind]) + (key_field or bytes(crypto.KEY_FIELD)) + os.urandom(crypto.NONCE)
    inner = sender.public.sign_pub + sender.sign(crypto._SIGN_DOMAIN + channel + header + body_bytes) + body_bytes
    nonce = header[-crypto.NONCE:]
    return header + ChaCha20Poly1305(key).encrypt(nonce, inner, crypto._AAD_DOMAIN + channel + header)


def encode(body) -> bytes:
    """JSON the way a modified app could send it (lone surrogates and all)."""
    return json.dumps(body, separators=(",", ":"), ensure_ascii=True).encode("ascii")


class EnvelopeFuzzTests(unittest.TestCase):
    """Without the keys: random and damaged envelopes."""

    def setUp(self):
        self.rng = random.Random(SEED)
        self.me = Identity.generate()
        self.other = Identity.generate()
        self.channel, self.lobby_key = os.urandom(16), os.urandom(32)
        self.epoch_id, self.epoch_key = crypto.new_epoch()
        self.samples = [
            crypto.pack_lobby(self.channel, self.lobby_key, self.other, {"t": "hello", "s": "0" * 16, "n": 1}),
            crypto.pack_room(self.channel, self.epoch_id, self.epoch_key, self.other,
                             {"t": "bye", "s": "0" * 16, "n": 2}),
            crypto.pack_direct(self.channel, self.me.public, self.other, {"t": "friend_no", "s": "0" * 16, "n": 3}),
        ]

    def open_everything(self, data):
        self.assertIsInstance(crypto.is_for_me(data, self.me), bool)
        for opener in (lambda: crypto.peek(data),
                       lambda: crypto.open_lobby(data, self.channel, self.lobby_key),
                       lambda: crypto.open_room(data, self.channel, {self.epoch_id: self.epoch_key}),
                       lambda: crypto.open_direct(data, self.channel, self.me)):
            try:
                opener()
            except crypto.BadEnvelope:
                pass

    def test_random_and_damaged_envelopes(self):
        for _ in range(400 * ROUNDS):
            roll = self.rng.random()
            if roll < 0.3:
                data = bytes(self.rng.getrandbits(8) for _ in range(self.rng.choice([0, 1, 2, 30, 133, 200, 1000])))
            else:
                data = bytearray(self.rng.choice(self.samples))
                if roll < 0.6:
                    for _ in range(self.rng.randint(1, 4)):
                        data[self.rng.randrange(len(data))] ^= 1 << self.rng.randrange(8)
                elif roll < 0.8:
                    data = data[:self.rng.randrange(len(data))]
                else:
                    data += os.urandom(self.rng.randint(1, 100))
                data = bytes(data)
            with self.subTest(seed=SEED, data=data[:40].hex()):
                self.open_everything(data)
        self.open_everything(b"\x01\x02" + os.urandom(crypto.MAX_ENVELOPE))   # too big
        self.open_everything(bytearray(self.samples[0]))
        self.open_everything("text, not bytes")

    def test_hostile_bodies_inside_valid_envelopes(self):
        """Correctly encrypted and signed, but the body is junk: still only BadEnvelope."""
        for body in HOSTILE_BODIES:
            for kind in (crypto.LOBBY, crypto.ROOM, crypto.DIRECT):
                key = self.lobby_key if kind == crypto.LOBBY else self.epoch_key
                field = self.epoch_id if kind == crypto.ROOM else None
                data = raw_envelope(kind, key, self.channel, self.other, body, field, self.me.public)
                with self.subTest(body=body[:30], kind=kind):
                    try:
                        if kind == crypto.LOBBY:
                            opened = crypto.open_lobby(data, self.channel, self.lobby_key)
                        elif kind == crypto.ROOM:
                            opened = crypto.open_room(data, self.channel, {self.epoch_id: self.epoch_key})
                        else:
                            opened = crypto.open_direct(data, self.channel, self.me)
                    except crypto.BadEnvelope:
                        continue
                    table = {crypto.LOBBY: protocol.LOBBY, crypto.ROOM: protocol.ROOM}.get(kind, protocol.DIRECT)
                    try:
                        protocol.validate(table, opened.body)
                    except BadMessage:
                        pass

    def test_sending_to_a_broken_key_fails_cleanly(self):
        for dh in BAD_DH:
            self.assertFalse(crypto.usable_dh(dh))
            with self.assertRaises(ValueError):
                crypto.pack_direct(self.channel, PublicIdentity(self.other.public.sign_pub, dh), self.me,
                                   {"t": "friend_no", "s": "0" * 16, "n": 1})
        self.assertTrue(crypto.usable_dh(self.other.public.dh_pub))


class ValidateFuzzTests(unittest.TestCase):
    def test_every_message_type_with_broken_fields(self):
        rng = random.Random(SEED + 1)
        tables = {"lobby": protocol.LOBBY, "direct": protocol.DIRECT, "room": protocol.ROOM}
        checked = 0
        for _ in range(ROUNDS):
            for table_name, table in tables.items():
                for kind in table:
                    good = valid_body(table, kind, rng)
                    clean = protocol.validate(table, good)
                    self.assertIsNotNone(clean, (table_name, kind))
                    for _ in range(60):
                        body = mutate(good, table, rng)
                        try:
                            clean = protocol.validate(table, json.loads(encode(body)))
                        except BadMessage:
                            continue
                        except Exception as exc:  # anything else is a bug
                            self.fail(f"seed {SEED}: {table_name} {body!r:.300}: {type(exc).__name__}: {exc}")
                        if clean is None:
                            continue
                        spec = {**protocol.COMMON, **table[clean["t"]]}
                        for name, value in clean.items():
                            if name != "t":
                                self.assertTrue(conforms(value, spec[name]),
                                                f"seed {SEED}: {clean['t']}.{name} = {value!r:.200}")
                        checked += 1
        self.assertGreater(checked, 50)

    def test_text_is_cleaned(self):
        clean = protocol.validate(protocol.ROOM, {"t": "profile", "s": "0" * 16, "n": 1,
                                                  "name": "Mal\ud800lory‮⁦​\x1b[1m!"})
        self.assertEqual(clean["name"], "Mallory[1m!")
        self.assertEqual(protocol.validate(protocol.ROOM, {"t": "profile", "s": "0" * 16, "n": 1,
                                                           "name": "Žofka 🎬 قمر"})["name"], "Žofka 🎬 قمر")


class CodeFuzzTests(unittest.TestCase):
    def test_random_codes(self):
        rng = random.Random(SEED + 2)
        alphabet = "0123456789ABCDEFGHJKMNPQRSTVWXYZabcdefghijklmnopqrstuvwxyz-@:/. \t Oo1Il\ud800"
        good = RoomCode(os.urandom(16), os.urandom(8)).text()
        for _ in range(500 * ROUNDS):
            roll = rng.random()
            if roll < 0.4:
                text = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 90)))
            elif roll < 0.7:
                text = "SW-" + "".join(rng.choice(alphabet[:32]) for _ in range(rng.randint(40, 50)))
            elif roll < 0.85:
                chars = list(good)
                chars[rng.randrange(len(chars))] = rng.choice(alphabet)
                text = "".join(chars)
            else:
                text = good + "@" + rng.choice(["", "http://", "https://x", "ftp://x.y", "https://[::1",
                                                "https://a.b:99999", "javascript:alert(1)", "x" * 700,
                                                "https://user:pw@host", "https://hést.cz"])
            with self.subTest(text=text[:60]):
                try:
                    parse(text)
                except CodeError:
                    pass


class WebSocketFuzzTests(unittest.TestCase):
    """A broken or hostile relay sending garbage frames: only WebSocket errors (or a timeout)."""

    def run_stream(self, data: bytes, expect_masked: bool):
        a, b = socket.socketpair()
        try:
            ws = WebSocket(b, b"", max_message=64 * 1024, mask_outgoing=not expect_masked,
                           expect_masked=expect_masked)

            def feed():
                try:
                    a.sendall(data)
                    a.shutdown(socket.SHUT_WR)
                except OSError:
                    pass
            writer = threading.Thread(target=feed, daemon=True)
            writer.start()
            for _ in range(20):
                try:
                    ws.recv(timeout=0.2)
                except (WebSocketError, socket.timeout):
                    break
            writer.join(2)
        finally:
            a.close()
            b.close()

    def test_garbage_frames(self):
        rng = random.Random(SEED + 3)
        frames = [b"\x82\x05hello", b"\x81\x02\xff\xfe", b"\x88\x01x", b"\x88\x00", b"\x89\x00", b"\x8a\x03abc",
                  b"\x02\x03abc\x80\x03def", b"\x00\x01x", b"\x82\x7e\xff\xff", b"\x82\x7f" + b"\xff" * 8,
                  b"\x82\x7f" + b"\x7f" + b"\xff" * 7, b"\xf2\x00", b"\x83\x00", b"\x89\x7e\x00\x80" + b"x" * 128]
        for _ in range(150 * ROUNDS):
            if rng.random() < 0.5:
                data = b"".join(rng.choice(frames) for _ in range(rng.randint(1, 4)))
            else:
                data = bytes(rng.getrandbits(8) for _ in range(rng.randint(1, 64)))
            with self.subTest(data=data[:30].hex()):
                self.run_stream(data, expect_masked=rng.random() < 0.3)


class HostileMemberTests(unittest.TestCase):
    """Mallory gets into the room with a modified app and sends anything she likes."""

    def setUp(self):
        self.rng = random.Random(SEED + 4)
        self.room = Room()
        self.alex = self.room.person("Alex")
        self.marek = self.room.person("Marek")
        self.mallory = self.room.person("Mallory")
        self.room.let_in(self.alex, self.marek, self.mallory)
        self.counter = 1000

    def tearDown(self):
        self.room.close()

    def body(self, table, kind):
        self.counter += 1
        body = valid_body(table, kind, self.rng)
        body["s"], body["n"] = "ab" * 8, self.counter
        if self.rng.random() < 0.8:
            body = mutate(body, table, self.rng)
            body["s"], body["n"] = "ab" * 8, self.counter
        return body

    def deliver(self, person, data):
        try:
            person.c.handle({"t": "_relay", "gen": person.c.session._gen, "data": data})
        except Exception as exc:
            self.fail(f"seed {SEED}: {person.name} crashed: {type(exc).__name__}: {exc}")

    def test_hostile_room_lobby_and_direct_messages(self):
        mal, ses = self.mallory.rooms.identity, self.mallory.c.session
        channel, lobby_key = ses.keys.channel, ses.keys.lobby_key
        for _ in range(250 * ROUNDS):
            roll = self.rng.random()
            target = self.rng.choice([self.alex, self.marek])
            if roll < 0.6:
                kind = self.rng.choice(list(protocol.ROOM))
                data = raw_envelope(crypto.ROOM, ses.epochs[ses.epoch_id], channel, mal,
                                    encode(self.body(protocol.ROOM, kind)), ses.epoch_id)
            elif roll < 0.75:
                kind = self.rng.choice(list(protocol.LOBBY))
                data = raw_envelope(crypto.LOBBY, lobby_key, channel, mal, encode(self.body(protocol.LOBBY, kind)))
            elif roll < 0.95:
                kind = self.rng.choice(list(protocol.DIRECT))
                data = raw_envelope(crypto.DIRECT, None, channel, mal, encode(self.body(protocol.DIRECT, kind)),
                                    recipient=target.rooms.identity.public)
            else:
                kind = crypto.ROOM
                data = raw_envelope(crypto.ROOM, ses.epochs[ses.epoch_id], channel, mal,
                                    self.rng.choice(HOSTILE_BODIES), ses.epoch_id)
            self.deliver(target, data)
            if self.rng.random() < 0.05:
                self.room.pump(0)
        # Nobody took over, and the room still works.
        self.assertTrue(self.alex.c.session.is_host)
        self.assertTrue(self.marek.c.connected)
        self.assertEqual(self.marek.c.session.host.key, self.alex.rooms.identity.public)
        self.alex.c.play()
        self.assertTrue(self.room.pump(until=lambda: self.marek.player.playing, timeout=10),
                        self.marek.ui.events[-10:])

    def test_a_broken_key_cant_break_the_room(self):
        """A knock with a key nobody can encrypt to is ignored, so it can't stop the room key changing."""
        code = parse(self.alex.code())
        keys = crypto.room_keys(code)
        host_card = self.alex.c.session
        for dh in BAD_DH:
            stranger = Identity.generate()
            self.counter += 1
            knock = {"t": "knock", "s": "cd" * 8, "n": self.counter, "challenge": base64.b64encode(
                host_card.challenge).decode(), "nonce": base64.b64encode(os.urandom(16)).decode(),
                "name": "Broken", "dh": base64.b64encode(dh).decode(), "app": "0.4.0", "v": host_card.protocol}
            self.deliver(self.alex, raw_envelope(crypto.DIRECT, None, keys.channel, stranger, encode(knock),
                                                 recipient=self.alex.rooms.identity.public))
        self.assertEqual(self.alex.c.session.waiting, {})
        self.alex.c.approve_all()
        old_epoch = self.marek.c.session.epoch_id
        self.alex.c.kick(self.mallory.id)          # a departure: everyone else gets a new key
        self.assertTrue(self.room.pump(until=lambda: self.marek.c.session.epoch_id != old_epoch, timeout=10))
        self.alex.c.pause()
        self.alex.c.play()
        self.assertTrue(self.room.pump(until=lambda: self.marek.player.playing, timeout=10))


    def test_floods_dont_make_the_host_flood(self):
        """However much Mallory sends, the host answers a little: the relay would drop everyone's
        messages from a host sending more than 20 a second."""
        alex = self.alex.c.session
        sent = []
        original = alex._send
        alex._send = lambda data: (sent.append(data), original(data))[1]

        def kinds():
            out = {}
            for data in sent:
                kind, _ = crypto.peek(data)
                if kind == crypto.ROOM:
                    try:
                        opened = crypto.open_room(data, alex.keys.channel, alex.epochs)
                        name = opened.body["t"]
                    except crypto.BadEnvelope:
                        name = "room?"
                else:
                    name = {crypto.LOBBY: "lobby", crypto.DIRECT: "direct"}[kind]
                out[name] = out.get(name, 0) + 1
            sent.clear()
            return out

        mal, ses = self.mallory.rooms.identity, self.mallory.c.session

        def from_mallory(kind, **fields):
            self.counter += 1
            body = {"t": kind, "s": "ab" * 8, "n": self.counter, **fields}
            return raw_envelope(crypto.ROOM, ses.epochs[ses.epoch_id], ses.keys.channel, mal, encode(body),
                                ses.epoch_id)

        self.room.pump(0.3)
        kinds()
        for i in range(100):
            self.deliver(self.alex, from_mallory("ping", id=float(i + 1)))
        self.assertLessEqual(kinds().get("pong", 0), 2)

        self.alex.settings.set("sync.host_only", True)
        for _ in range(100):
            self.deliver(self.alex, from_mallory("req", action="pause"))
        self.assertLessEqual(kinds().get("reject", 0), 1)

        for i in range(60):
            self.deliver(self.alex, from_mallory("profile", name=f"Mallory {i}"))
        self.room.pump(1.5)
        self.assertLessEqual(kinds().get("members", 0), 2)
        self.assertEqual(self.marek.c.session.members[self.mallory.id].name, "Mallory 59")

        self.alex.settings.set("room.policy", "friends_only")
        code = crypto.room_keys(parse(self.alex.code()))
        for _ in range(60):
            stranger = Identity.generate()
            self.counter += 1
            knock = {"t": "knock", "s": "cd" * 8, "n": self.counter, "challenge": base64.b64encode(
                alex.challenge).decode(), "nonce": base64.b64encode(os.urandom(16)).decode(), "name": "Spam",
                "dh": base64.b64encode(stranger.public.dh_pub).decode(), "app": "0.4.0", "v": alex.protocol}
            self.deliver(self.alex, raw_envelope(crypto.DIRECT, None, code.channel, stranger, encode(knock),
                                                 recipient=self.alex.rooms.identity.public))
        self.assertLessEqual(kinds().get("direct", 0), session_module.DENY_BURST + 2)


class HostileMailTests(unittest.TestCase):
    def setUp(self):
        self.rng = random.Random(SEED + 5)
        self.room = Room()
        self.alex = self.room.person("Alex")
        self.marek = self.room.person("Marek")
        self.stranger = Identity.generate()
        a, m = self.alex.c.friends, self.marek.c.friends
        self.alex.rooms.store.add_friend(self.marek.rooms.identity.public, "Marek", m.mailbox_id)
        self.marek.rooms.store.add_friend(self.alex.rooms.identity.public, "Alex", a.mailbox_id)

    def tearDown(self):
        self.room.close()

    def test_hostile_mail(self):
        friends = self.alex.c.friends
        kinds = sorted(session_module_kinds())
        for i in range(300 * ROUNDS):
            sender = self.marek.rooms.identity if self.rng.random() < 0.6 else self.stranger
            kind = self.rng.choice(kinds)
            body = valid_body(protocol.DIRECT, kind, self.rng)
            if kind in ("hosting", "hosting_end", "who_hosts", "unfriend"):
                body["ts"] = time.time()
            if kind == "hosting" and self.rng.random() < 0.5:
                body["code"] = self.marek.c.session.store.my_code(self.marek.rooms.identity).text()
            if kind == "friend_ok" and self.rng.random() < 0.5:
                body["dh"] = base64.b64encode(self.rng.choice(BAD_DH)).decode()
            if self.rng.random() < 0.7:
                body = mutate(body, protocol.DIRECT, self.rng)
            body["s"], body["n"] = "ef" * 8, 1000 + i
            data = raw_envelope(crypto.DIRECT, None, friends.mailbox_bytes, sender, encode(body),
                                recipient=self.alex.rooms.identity.public)
            try:
                self.alex.c.handle({"t": "_mail", "gen": friends._gen, "data": data})
            except Exception as exc:
                self.fail(f"seed {SEED}: {body!r:.300}: {type(exc).__name__}: {exc}")
            if not self.alex.rooms.store.is_friend(self.marek.rooms.identity.public):   # an "unfriend" got through
                self.alex.rooms.store.add_friend(self.marek.rooms.identity.public, "Marek",
                                                 self.marek.c.friends.mailbox_id)
        self.assertEqual([f.key for f in friends.friend_list()], [self.marek.rooms.identity.public])
        self.assertTrue(self.alex.rooms.store.save())


def session_module_kinds():
    from syncwatch.room.friends import MAIL_KINDS
    return MAIL_KINDS | set(session_module.FRIEND_KINDS)


if __name__ == "__main__":
    unittest.main()
