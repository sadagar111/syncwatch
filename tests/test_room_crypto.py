"""Room codes, identities and message encryption (syncwatch/room)."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305  # noqa: E402

from syncwatch.room import codes, crypto  # noqa: E402
from syncwatch.room.identity import Identity, PublicIdentity, load_or_create  # noqa: E402
from syncwatch.room.store import RoomStore  # noqa: E402


class Base32Tests(unittest.TestCase):
    def test_round_trip(self):
        for length in (1, 5, 8, 16, 27, 32):
            data = os.urandom(length)
            self.assertEqual(codes.b32decode(codes.b32encode(data), length), data)

    def test_look_alikes_and_bad_characters(self):
        text = codes.b32encode(bytes(5))           # "00000000"
        self.assertEqual(codes.b32decode(text.replace("0", "O"), 5), bytes(5))
        self.assertEqual(codes.b32decode("11111111", 5), codes.b32decode("ILIL1LI1", 5))
        with self.assertRaises(codes.CodeError):
            codes.b32decode("0000000U", 5)       # U is not in the alphabet
        with self.assertRaises(codes.CodeError):
            codes.b32decode("0000000", 5)        # too short


class RoomCodeTests(unittest.TestCase):
    def setUp(self):
        self.code = codes.RoomCode(os.urandom(16), os.urandom(8))

    def test_round_trip_and_forgiving_input(self):
        text = self.code.text()
        self.assertTrue(text.startswith("SW-"))
        self.assertEqual(codes.parse(text), self.code)
        self.assertEqual(codes.parse("  " + text.lower() + "\n"), self.code)
        self.assertEqual(codes.parse(text.replace("-", " ")), self.code)
        self.assertEqual(codes.parse(text[3:].replace("-", "")), self.code)  # without "SW-"

    def test_typos_are_caught(self):
        text = self.code.text()
        body = text[3:]
        for index in range(0, len(body), 3):
            char = body[index]
            if char == "-":
                continue
            other = "1" if char != "1" else "2"
            broken = "SW-" + body[:index] + other + body[index + 1:]
            with self.subTest(index=index), self.assertRaises(codes.CodeError):
                codes.parse(broken)
        with self.assertRaises(codes.CodeError):
            codes.parse(text[:-4])                 # a piece missing
        with self.assertRaises(codes.CodeError):
            codes.parse("")

    def test_relay_suffix(self):
        code = codes.RoomCode(self.code.secret, self.code.host_commit, "https://relay.example.org")
        self.assertEqual(codes.parse(code.text()), code)
        self.assertEqual(codes.parse(self.code.text() + "@http://127.0.0.1:8787").relay, "http://127.0.0.1:8787")
        for bad in ("@http://example.org", "@ftp://example.org", "@https://user:pw@example.org", "@javascript:x"):
            with self.subTest(bad=bad), self.assertRaises(codes.CodeError):
                codes.parse(self.code.text() + bad)

    def test_str_hides_the_secret(self):
        self.assertNotIn(self.code.secret.hex(), str(self.code) + repr(self.code))

    def test_newer_version_is_explained(self):
        payload = bytes([2]) + self.code.secret + self.code.host_commit
        text = "SW-" + codes.b32encode(payload + codes._checksum(payload))
        with self.assertRaisesRegex(codes.CodeError, "newer SyncWatch"):
            codes.parse(text)


class IdentityTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="sw-id-"))

    def test_create_save_load(self):
        path = self.dir / "identity.json"
        first, problem = load_or_create(path)
        self.assertIsNone(problem)
        again, problem = load_or_create(path)
        self.assertIsNone(problem)
        self.assertEqual(first.public, again.public)
        self.assertEqual(first.mailbox_secret, again.mailbox_secret)
        self.assertRegex(first.public.short_id, r"^[0-9A-Z]{4}-[0-9A-Z]{4}$")
        self.assertNotIn(json.loads(path.read_text())["sign"], repr(first))

    def test_damaged_file_is_kept_and_replaced(self):
        path = self.dir / "identity.json"
        path.write_text("{broken", encoding="utf-8")
        identity, problem = load_or_create(path)
        self.assertIsNotNone(problem)
        self.assertTrue(list(self.dir.glob("identity.json.damaged-*")))
        self.assertEqual(load_or_create(path)[0].public, identity.public)

    def test_public_identity_json_and_checks(self):
        identity = Identity.generate()
        copy = PublicIdentity.from_json(json.loads(json.dumps(identity.public.to_json())))
        self.assertEqual(copy, identity.public)
        self.assertEqual(copy.key_id, identity.public.fingerprint[:8])
        signature = identity.sign(b"hello")
        self.assertTrue(copy.verify(signature, b"hello"))
        self.assertFalse(copy.verify(signature, b"hellO"))
        with self.assertRaises(ValueError):
            PublicIdentity(b"short", bytes(32))


def body(kind="test", **fields):
    return {"t": kind, **fields}


class EnvelopeTests(unittest.TestCase):
    def setUp(self):
        self.host = Identity.generate()
        self.guest = Identity.generate()
        self.stranger = Identity.generate()
        self.code = codes.RoomCode(os.urandom(16), self.host.public.key_id)
        self.keys = crypto.room_keys(self.code)
        self.channel = self.keys.channel

    def test_keys_are_derived_from_the_code(self):
        self.assertEqual(crypto.room_keys(self.code), self.keys)
        other = crypto.room_keys(codes.RoomCode(os.urandom(16), self.host.public.key_id))
        self.assertNotEqual(other.channel, self.keys.channel)
        self.assertNotEqual(other.lobby_key, self.keys.lobby_key)
        self.assertRegex(self.keys.channel_id, r"^[0-9a-f]{32}$")
        self.assertEqual(len(crypto.mailbox_channel(self.guest.mailbox_secret)), 16)

    def test_lobby_round_trip(self):
        data = crypto.pack_lobby(self.channel, self.keys.lobby_key, self.guest, body("hello", name="Marek"))
        opened = crypto.open_lobby(data, self.channel, self.keys.lobby_key)
        self.assertEqual(opened.body, {"t": "hello", "name": "Marek"})
        self.assertEqual(opened.sender, self.guest.public.sign_pub)
        self.assertEqual(opened.kind, crypto.LOBBY)

    def test_room_round_trip_and_old_keys(self):
        epoch_id, key = crypto.new_epoch()
        data = crypto.pack_room(self.channel, epoch_id, key, self.host, body("state", pos=1234))
        self.assertEqual(crypto.open_room(data, self.channel, {epoch_id: key}).body["pos"], 1234)
        with self.assertRaises(crypto.BadEnvelope):
            crypto.open_room(data, self.channel, {})                         # not let in
        new_id, new_key = crypto.new_epoch()
        with self.assertRaises(crypto.BadEnvelope):
            crypto.open_room(data, self.channel, {new_id: new_key})          # after a rekey
        with self.assertRaises(crypto.BadEnvelope):
            crypto.open_room(data, self.channel, {epoch_id: new_key})        # wrong key, right id

    def test_direct_only_for_the_recipient(self):
        data = crypto.pack_direct(self.channel, self.guest.public, self.host, body("welcome", key="x"))
        self.assertTrue(crypto.is_for_me(data, self.guest))
        self.assertFalse(crypto.is_for_me(data, self.stranger))
        self.assertEqual(crypto.open_direct(data, self.channel, self.guest).sender, self.host.public.sign_pub)
        with self.assertRaisesRegex(crypto.BadEnvelope, "someone else"):
            crypto.open_direct(data, self.channel, self.stranger)
        # Re-addressing it to the stranger doesn't help: the address is locked in too.
        forged = data[:2] + self.stranger.public.key_id + data[10:]
        with self.assertRaises(crypto.BadEnvelope):
            crypto.open_direct(forged, self.channel, self.stranger)

    def test_wrong_room_wrong_key_and_tampering(self):
        data = crypto.pack_lobby(self.channel, self.keys.lobby_key, self.guest, body("hello"))
        other = crypto.room_keys(codes.RoomCode(os.urandom(16), self.host.public.key_id))
        with self.assertRaises(crypto.BadEnvelope):
            crypto.open_lobby(data, other.channel, self.keys.lobby_key)        # copied into another room
        with self.assertRaises(crypto.BadEnvelope):
            crypto.open_lobby(data, self.channel, other.lobby_key)
        for index in range(len(data)):
            if index in (0, 1):
                continue  # version/kind bytes give their own errors, checked below
            tampered = bytearray(data)
            tampered[index] ^= 0x01
            with self.subTest(index=index), self.assertRaises(crypto.BadEnvelope):
                crypto.open_lobby(bytes(tampered), self.channel, self.keys.lobby_key)
        with self.assertRaisesRegex(crypto.BadEnvelope, "version"):
            crypto.open_lobby(bytes([9]) + data[1:], self.channel, self.keys.lobby_key)
        with self.assertRaises(crypto.BadEnvelope):
            crypto.open_lobby(data[:50], self.channel, self.keys.lobby_key)
        with self.assertRaises(crypto.BadEnvelope):
            crypto.open_lobby(b"x" * (crypto.MAX_ENVELOPE + 1), self.channel, self.keys.lobby_key)

    def test_someone_with_the_code_cant_sign_as_someone_else(self):
        # A stranger knows the lobby key (from the code) but not the host's signing key.
        payload = b'{"t":"card","name":"Alex"}'
        header = bytes([crypto.VERSION, crypto.LOBBY]) + bytes(8) + os.urandom(12)
        signature = self.stranger.sign(crypto._SIGN_DOMAIN + self.channel + header + payload)
        data = header + ChaCha20Poly1305(self.keys.lobby_key).encrypt(
            header[-12:], self.host.public.sign_pub + signature + payload,
            crypto._AAD_DOMAIN + self.channel + header)
        with self.assertRaisesRegex(crypto.BadEnvelope, "signature"):
            crypto.open_lobby(data, self.channel, self.keys.lobby_key)

    def test_signed_garbage_is_still_rejected(self):
        for payload in (b"not json", b"[1,2]", b'{"no_type":1}', b'{"t":"x","n":NaN}', b"\xff\xfe"):
            header = bytes([crypto.VERSION, crypto.LOBBY]) + bytes(8) + os.urandom(12)
            signature = self.guest.sign(crypto._SIGN_DOMAIN + self.channel + header + payload)
            data = header + ChaCha20Poly1305(self.keys.lobby_key).encrypt(
                header[-12:], self.guest.public.sign_pub + signature + payload,
                crypto._AAD_DOMAIN + self.channel + header)
            with self.subTest(payload=payload), self.assertRaises(crypto.BadEnvelope):
                crypto.open_lobby(data, self.channel, self.keys.lobby_key)

    def test_broken_throwaway_key(self):
        data = bytearray(crypto.pack_direct(self.channel, self.guest.public, self.host, body("x")))
        data[10:42] = bytes(32)  # an all-zero ("low order") X25519 key
        with self.assertRaises(crypto.BadEnvelope):
            crypto.open_direct(bytes(data), self.channel, self.guest)

    def test_bodies_must_be_reasonable(self):
        with self.assertRaises(ValueError):
            crypto.pack_lobby(self.channel, self.keys.lobby_key, self.guest, {"no": "type"})
        with self.assertRaises(ValueError):
            crypto.pack_lobby(self.channel, self.keys.lobby_key, self.guest, body("big", x="a" * crypto.MAX_BODY))
        with self.assertRaises(ValueError):
            crypto.pack_lobby(self.channel, self.keys.lobby_key, self.guest, body("nan", x=float("nan")))


class ReplayGuardTests(unittest.TestCase):
    def test_only_new_counters_pass(self):
        guard = crypto.ReplayGuard(max_entries=3)
        a, b = b"a" * 32, b"b" * 32
        self.assertTrue(guard.accept(a, "s1", 1))
        self.assertTrue(guard.accept(a, "s1", 5))
        self.assertFalse(guard.accept(a, "s1", 5))
        self.assertFalse(guard.accept(a, "s1", 4))
        self.assertTrue(guard.accept(a, "s2", 1))     # a new session of the same person
        self.assertTrue(guard.accept(b, "s1", 1))
        guard.forget(a)
        self.assertTrue(guard.accept(a, "s1", 2))
        for index in range(5):                         # old entries are forgotten after a while
            guard.accept(bytes([index]) * 32, "x", 1)
        self.assertEqual(len(guard._last), 3)


class RoomStoreTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="sw-store-"))
        self.now = 1_000_000.0
        self.me = Identity.generate()
        self.a, self.b = Identity.generate().public, Identity.generate().public

    def store(self):
        return RoomStore(self.dir / "rooms.json", wall=lambda: self.now)

    def test_saved_and_loaded(self):
        store = self.store()
        code = store.my_code(self.me)
        self.assertEqual(store.my_code(self.me).text(), code.text())    # the same code every time
        store.add_friend(self.a, "Marek", "ab" * 16)
        store.block(self.b, "Spammer")
        store.remember_in_room([self.a])
        store.save()
        again = self.store()
        self.assertIsNone(again.problem)
        self.assertEqual(again.my_code(self.me).text(), code.text())
        self.assertTrue(again.is_friend(self.a))
        self.assertTrue(again.is_blocked(self.b))
        self.assertTrue(again.was_recently_in_room(self.a))
        self.assertNotEqual(again.new_code(self.me).text(), code.text())

    def test_recent_members(self):
        store = self.store()
        store.remember_in_room([self.a])
        self.assertTrue(store.was_recently_in_room(self.a))
        self.assertFalse(store.was_recently_in_room(self.b))
        # Someone whose key id matches but whose full fingerprint doesn't is someone else.
        store.recent = {self.a.key_id + bytes(24): self.now}
        self.assertFalse(store.was_recently_in_room(self.a))
        store.remember_in_room([self.a])
        store.forget_recent(self.a.key_id)
        self.assertFalse(store.was_recently_in_room(self.a))
        store.remember_in_room([self.a])
        self.now += 10 * 60 + 1
        self.assertFalse(store.was_recently_in_room(self.a))
        store.remember_in_room([self.a])
        store.block(self.a, "Marek")
        self.assertFalse(store.was_recently_in_room(self.a))

    def test_damaged_file_starts_fresh(self):
        (self.dir / "rooms.json").write_text("{not json", encoding="utf-8")
        store = self.store()
        self.assertIsNotNone(store.problem)
        self.assertEqual(store.friends, {})
        self.assertTrue(any(p.name.startswith("rooms.json.damaged-") for p in self.dir.iterdir()))
        self.assertTrue(store.save())
        self.assertIsNone(self.store().problem)


if __name__ == "__main__":
    unittest.main()
