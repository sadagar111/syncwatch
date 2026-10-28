"""Locking and unlocking every message that goes through the relay.

Each message is an *envelope*: a short header, then the encrypted contents.
Inside the encryption: the sender's public signing key, their signature, and
the message itself (JSON). So every message is

* **secret**: encrypted with ChaCha20-Poly1305. The relay, and anyone watching
  it, sees only noise and the message size;
* **tamper-proof**: changing a single bit makes it fail to open;
* **signed**: the signature (Ed25519) proves who sent it, even to other people
  in the same room who share the same key;
* **bound to its place**: the signature and encryption cover the channel and
  the header, so a message can't be copied into another room or re-sent to
  someone else.

Three kinds of envelope, by which key locks them:

* ``LOBBY``: the key comes from the room code. Used before you're let in
  (asking to join, the host's "room card").
* ``ROOM``: the room key (an *epoch* key). The host makes a new one whenever
  someone joins or leaves and hands it only to the people in the room, so a
  leaked code or someone who left can't read along.
* ``DIRECT``: locked for one person's encryption key (X25519 with a fresh
  throwaway key per message, "sealed box"). Used for the room key itself, for
  "you're in", and for friends' mailboxes.

Envelope layout (bytes)::

    0      version (1)
    1      kind (1 lobby, 2 room, 3 direct)
    2..9   lobby: zeros · room: epoch id · direct: recipient's key id
    direct only: 32-byte throwaway public key
    then   12-byte nonce, then the ciphertext (incl. 16-byte tag)

Inside (after decrypting)::

    32-byte sender signing key · 64-byte signature · JSON body

Replays (re-sending a recorded message) are handled one level up: every body
carries the sender's session id and a counter, checked by ``ReplayGuard``.
"""
from __future__ import annotations

import collections
import json
import os
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from .codes import RoomCode
from .identity import Identity, PublicIdentity

VERSION = 1
LOBBY, ROOM, DIRECT = 1, 2, 3
KEY_FIELD = 8
NONCE = 12
TAG = 16
PUB = 32
SIG = 64
MAX_ENVELOPE = 64 * 1024          # the relay refuses anything bigger
MAX_BODY = MAX_ENVELOPE - 256
_ZERO_FIELD = bytes(KEY_FIELD)
_SIGN_DOMAIN = b"SyncWatch msg v1\x00"
_AAD_DOMAIN = b"SyncWatch env v1\x00"


class BadEnvelope(ValueError):
    """Not a message we can open: damaged, forged, for someone else, or not for this room."""


def hkdf(ikm: bytes, salt: bytes, info: bytes, length: int = 32) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=length, salt=salt, info=info).derive(ikm)


@dataclass(frozen=True)
class RoomKeys:
    channel: bytes      # 16 bytes: which relay channel the room uses
    lobby_key: bytes    # 32 bytes

    @property
    def channel_id(self) -> str:
        return self.channel.hex()


def room_keys(code: RoomCode) -> RoomKeys:
    ikm = code.secret + code.host_commit
    salt = b"SyncWatch room v1"
    return RoomKeys(hkdf(ikm, salt, b"channel", 16), hkdf(ikm, salt, b"lobby", 32))


def mailbox_channel(mailbox_secret: bytes) -> bytes:
    """The relay channel where friends leave messages for this person."""
    return hkdf(mailbox_secret, b"SyncWatch mailbox v1", b"channel", 16)


def new_epoch() -> Tuple[bytes, bytes]:
    """A fresh room key: (8-byte epoch id, 32-byte key)."""
    return os.urandom(KEY_FIELD), ChaCha20Poly1305.generate_key()


@dataclass(frozen=True)
class Opened:
    kind: int
    key_field: bytes        # epoch id (room) / recipient key id (direct) / zeros (lobby)
    sender: bytes           # the sender's 32-byte signing key (proven by the signature)
    body: Dict[str, Any]


# ----------------------------------------------------------------------------- packing
def _encode_body(body: Dict[str, Any]) -> bytes:
    if not isinstance(body, dict) or not isinstance(body.get("t"), str):
        raise ValueError("a message body is a dict with a 't' (type) field")
    data = json.dumps(body, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")
    if len(data) > MAX_BODY:
        raise ValueError(f"message too large ({len(data)} bytes)")
    return data


def _inner(channel: bytes, header: bytes, sender: Identity, body: Dict[str, Any]) -> bytes:
    data = _encode_body(body)
    signature = sender.sign(_SIGN_DOMAIN + channel + header + data)
    return sender.public.sign_pub + signature + data


def _seal(key: bytes, header: bytes, channel: bytes, plaintext: bytes) -> bytes:
    nonce = header[-NONCE:]
    return header + ChaCha20Poly1305(key).encrypt(nonce, plaintext, _AAD_DOMAIN + channel + header)


def pack_lobby(channel: bytes, lobby_key: bytes, sender: Identity, body: Dict[str, Any]) -> bytes:
    header = bytes([VERSION, LOBBY]) + _ZERO_FIELD + os.urandom(NONCE)
    return _seal(lobby_key, header, channel, _inner(channel, header, sender, body))


def pack_room(channel: bytes, epoch_id: bytes, epoch_key: bytes, sender: Identity, body: Dict[str, Any]) -> bytes:
    if len(epoch_id) != KEY_FIELD:
        raise ValueError("epoch id must be 8 bytes")
    header = bytes([VERSION, ROOM]) + epoch_id + os.urandom(NONCE)
    return _seal(epoch_key, header, channel, _inner(channel, header, sender, body))


def _direct_key(shared: bytes, ephemeral_pub: bytes, recipient_dh: bytes, channel: bytes) -> bytes:
    return hkdf(shared, b"SyncWatch direct v1", ephemeral_pub + recipient_dh + channel)


def pack_direct(channel: bytes, recipient: PublicIdentity, sender: Identity, body: Dict[str, Any]) -> bytes:
    ephemeral = X25519PrivateKey.generate()
    ephemeral_pub = ephemeral.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    shared = ephemeral.exchange(X25519PublicKey.from_public_bytes(recipient.dh_pub))
    key = _direct_key(shared, ephemeral_pub, recipient.dh_pub, channel)
    header = bytes([VERSION, DIRECT]) + recipient.key_id + ephemeral_pub + os.urandom(NONCE)
    return _seal(key, header, channel, _inner(channel, header, sender, body))


# ----------------------------------------------------------------------------- unpacking
def peek(data: bytes) -> Tuple[int, bytes]:
    """(kind, key field) from the header, without opening anything. Raises BadEnvelope."""
    if not isinstance(data, (bytes, bytearray)) or len(data) > MAX_ENVELOPE:
        raise BadEnvelope("not an envelope")
    if len(data) < 2 + KEY_FIELD + NONCE + TAG + PUB + SIG:
        raise BadEnvelope("too short")
    if data[0] != VERSION:
        raise BadEnvelope(f"envelope version {data[0]} (newer SyncWatch?)")
    kind = data[1]
    if kind not in (LOBBY, ROOM, DIRECT):
        raise BadEnvelope("unknown kind")
    return kind, bytes(data[2:2 + KEY_FIELD])


def _header_size(kind: int) -> int:
    return 2 + KEY_FIELD + (PUB if kind == DIRECT else 0) + NONCE


def _unseal(key: bytes, data: bytes, kind: int, channel: bytes) -> Opened:
    size = _header_size(kind)
    if len(data) < size + TAG + PUB + SIG:
        raise BadEnvelope("too short")
    header, ciphertext = bytes(data[:size]), bytes(data[size:])
    try:
        plaintext = ChaCha20Poly1305(key).decrypt(header[-NONCE:], ciphertext, _AAD_DOMAIN + channel + header)
    except InvalidTag:
        raise BadEnvelope("can't be opened with this key (wrong key, wrong room, or tampered with)") from None
    sender, signature, body_bytes = plaintext[:PUB], plaintext[PUB:PUB + SIG], plaintext[PUB + SIG:]
    try:
        Ed25519PublicKey.from_public_bytes(sender).verify(signature, _SIGN_DOMAIN + channel + header + body_bytes)
    except Exception:
        raise BadEnvelope("bad signature") from None
    try:
        body = json.loads(body_bytes.decode("utf-8"), parse_constant=_no_constants)
    except (UnicodeDecodeError, ValueError) as exc:
        raise BadEnvelope(f"not JSON: {exc}") from None
    if not isinstance(body, dict) or not isinstance(body.get("t"), str):
        raise BadEnvelope("not a message object")
    return Opened(kind, header[2:2 + KEY_FIELD], sender, body)


def _no_constants(name: str):
    raise ValueError(f"{name} is not allowed")


def open_lobby(data: bytes, channel: bytes, lobby_key: bytes) -> Opened:
    kind, field = peek(data)
    if kind != LOBBY or field != _ZERO_FIELD:
        raise BadEnvelope("not a lobby message")
    return _unseal(lobby_key, data, kind, channel)


def open_room(data: bytes, channel: bytes, epoch_keys: Dict[bytes, bytes]) -> Opened:
    kind, field = peek(data)
    if kind != ROOM:
        raise BadEnvelope("not a room message")
    key = epoch_keys.get(field)
    if key is None:
        raise BadEnvelope("room key we don't have (not let in yet, or an old one)")
    return _unseal(key, data, kind, channel)


def open_direct(data: bytes, channel: bytes, me: Identity) -> Opened:
    kind, field = peek(data)
    if kind != DIRECT:
        raise BadEnvelope("not a direct message")
    if field != me.public.key_id:
        raise BadEnvelope("for someone else")
    ephemeral_pub = bytes(data[2 + KEY_FIELD:2 + KEY_FIELD + PUB])
    try:
        shared = me.exchange(ephemeral_pub)
    except Exception:  # e.g. a deliberately broken ("low order") key
        raise BadEnvelope("bad throwaway key") from None
    key = _direct_key(shared, ephemeral_pub, me.public.dh_pub, channel)
    return _unseal(key, data, kind, channel)


def is_for_me(data: bytes, me: Identity) -> bool:
    """Cheap check whether a direct message is addressed to me (before trying to open it)."""
    try:
        kind, field = peek(data)
    except BadEnvelope:
        return False
    return kind == DIRECT and field == me.public.key_id


# ----------------------------------------------------------------------------- replays
class ReplayGuard:
    """Drops messages that were already seen, so a recording can't be played back.

    Every sender numbers its messages: a random session id per app start plus a
    counter. A message is accepted only if its counter is higher than the last
    one accepted from that sender and session.
    """

    def __init__(self, max_entries: int = 1024):
        self._last: "collections.OrderedDict[Tuple[bytes, str], int]" = collections.OrderedDict()
        self._max = max_entries

    def accept(self, sender: bytes, session: str, seq: int) -> bool:
        key = (bytes(sender), session)
        last = self._last.get(key)
        if last is not None and seq <= last:
            return False
        self._last[key] = seq
        self._last.move_to_end(key)
        while len(self._last) > self._max:
            self._last.popitem(last=False)
        return True

    def forget(self, sender: Optional[bytes] = None) -> None:
        if sender is None:
            self._last.clear()
            return
        for key in [k for k in self._last if k[0] == sender]:
            del self._last[key]
