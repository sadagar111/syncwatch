"""Room codes: what the host copies and friends paste to join.

A code holds three things (27 bytes, written as 44 letters and digits):

* a version number (1),
* the room secret (16 random bytes). Everything else about the room is derived
  from it: which relay channel to use and the key that encrypts the lobby,
* 8 bytes of the host's identity fingerprint. Only the real host has keys that
  match it, so nobody else who knows the code can pretend to be the host,
* a 2-byte checksum, so a code with a typo or a missing piece is recognised as
  such ("check the code") instead of quietly leading nowhere.

Written in Crockford's base32: digits and capital letters without I, L, O and
U, so it reads unambiguously and case doesn't matter. Dashes and spaces are
ignored when pasting. A room on a different relay server than the built-in one
gets ``@<relay address>`` appended.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Optional

ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_DECODE = {char: value for value, char in enumerate(ALPHABET)}
_DECODE.update({"I": 1, "L": 1, "O": 0})  # look-alikes people may type

CODE_VERSION = 1
SECRET_BYTES = 16
COMMIT_BYTES = 8
CHECK_BYTES = 2
PREFIX = "SW"


class CodeError(ValueError):
    """The text isn't a valid room code. The message says why, in plain words."""


def b32encode(data: bytes) -> str:
    bits = int.from_bytes(data, "big")
    count = (len(data) * 8 + 4) // 5
    bits <<= count * 5 - len(data) * 8
    return "".join(ALPHABET[(bits >> (5 * (count - 1 - i))) & 31] for i in range(count))


def b32decode(text: str, length: int) -> bytes:
    """Base32 text -> exactly ``length`` bytes. Raises CodeError."""
    count = (length * 8 + 4) // 5
    if len(text) != count:
        raise CodeError(f"the code should have {count} letters and digits, this one has {len(text)}")
    bits = 0
    for char in text:
        value = _DECODE.get(char)
        if value is None:
            raise CodeError(f"'{char}' can't be part of a code")
        bits = (bits << 5) | value
    extra = count * 5 - length * 8
    if bits & ((1 << extra) - 1):
        raise CodeError("the code doesn't end the way codes end. Check the last characters")
    return (bits >> extra).to_bytes(length, "big")


def group(text: str, size: int = 4) -> str:
    return "-".join(text[i:i + size] for i in range(0, len(text), size))


def _checksum(payload: bytes) -> bytes:
    return hashlib.sha256(b"SyncWatch room code\x00" + payload).digest()[:CHECK_BYTES]


@dataclass(frozen=True)
class RoomCode:
    secret: bytes          # 16 random bytes
    host_commit: bytes     # first 8 bytes of the host's identity fingerprint
    relay: str = ""        # "" = the built-in relay

    def __post_init__(self) -> None:
        if len(self.secret) != SECRET_BYTES or len(self.host_commit) != COMMIT_BYTES:
            raise CodeError("room code parts have the wrong size")

    def text(self) -> str:
        payload = bytes([CODE_VERSION]) + self.secret + self.host_commit
        code = f"{PREFIX}-" + group(b32encode(payload + _checksum(payload)))
        return f"{code}@{self.relay}" if self.relay else code

    def __str__(self) -> str:  # never log the secret by accident
        return f"RoomCode(host={self.host_commit.hex()})"

    __repr__ = __str__


_SEPARATORS = re.compile(r"[\s\-_.]+")


def parse(text: str) -> RoomCode:
    """Pasted text -> RoomCode. Raises CodeError with a plain-language reason."""
    text = (text or "").strip()
    if not text:
        raise CodeError("paste the room code first")
    if len(text) > 600:
        raise CodeError("that's far too long for a room code")
    code_part, _, relay = text.partition("@")
    relay = relay.strip()
    if relay:
        relay = check_relay_address(relay)
    letters = _SEPARATORS.sub("", code_part).upper()
    size = 1 + SECRET_BYTES + COMMIT_BYTES + CHECK_BYTES
    expected = (size * 8 + 4) // 5
    if letters.startswith(PREFIX) and len(letters) == expected + len(PREFIX):
        letters = letters[len(PREFIX):]  # (pasting without the "SW-" works too)
    raw = b32decode(letters, size)
    payload, check = raw[:-CHECK_BYTES], raw[-CHECK_BYTES:]
    if payload[0] != CODE_VERSION:
        raise CodeError("this room code is from a newer SyncWatch. Update SyncWatch first")
    if check != _checksum(payload):
        raise CodeError("the code has a typo or is incomplete. Copy it again")
    return RoomCode(payload[1:1 + SECRET_BYTES], payload[1 + SECRET_BYTES:], relay)


_RELAY = re.compile(r"^(https|http)://([A-Za-z0-9.-]{1,253})(:\d{1,5})?/?$")
_LOCAL_HOSTS = ("127.0.0.1", "localhost")


def check_relay_address(address: str) -> str:
    """A relay address must be https://host[:port] (http only for this PC, for testing)."""
    address = address.strip()
    match = _RELAY.match(address)
    if not match:
        raise CodeError("the relay address in the code isn't a web address like https://example.com")
    scheme, host = match.group(1), match.group(2).lower()
    if scheme == "http" and host not in _LOCAL_HOSTS:
        raise CodeError("relay addresses must start with https://")
    if match.group(3) and not 0 < int(match.group(3)[1:]) <= 65535:
        raise CodeError("the relay address in the code has an impossible port number")
    return address.rstrip("/")


def same_relay(a: Optional[str], b: Optional[str]) -> bool:
    return (a or "").rstrip("/").lower() == (b or "").rstrip("/").lower()
