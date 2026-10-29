"""Every message SyncWatch apps send each other, and strict checking of them.

A message body is a JSON object ``{"t": "<type>", "s": <sender session>,
"n": <counter>, ...fields}``. It travels inside an encrypted, signed envelope
(syncwatch/room/crypto.py), but signed doesn't mean sensible: a friend's app,
or someone who modified theirs, could still send odd data. So everything
received is checked against the tables below before the app acts on it: wrong
types, missing fields, absurd numbers, over-long text, broken base64 and
unknown message types are all rejected.

Three tables, by the envelope a message must arrive in (crypto.LOBBY,
crypto.DIRECT, crypto.ROOM): a message type that turns up in the wrong kind of
envelope is rejected too.
"""
from __future__ import annotations

import base64
import binascii
import math
import re
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

MAX_POSITION_MS = 48 * 3600 * 1000          # nothing is longer than two days
MAX_AVATAR_BYTES = 40_000                    # what goes over the network (re-encoded smaller if needed)
MAX_AVATAR_CHARS = (MAX_AVATAR_BYTES + 2) // 3 * 4
MAX_PEOPLE = 64
MAX_CLOCK = 1 << 53                         # milliseconds; fits a JSON number exactly
POLICIES = ("ask", "friends_direct", "friends_only", "open")


class BadMessage(ValueError):
    pass


@dataclass(frozen=True)
class B64:
    """Base64 text that must decode to exactly ``size`` bytes (returned as bytes)."""
    size: int


@dataclass(frozen=True)
class Hex:
    """Lower-case hex text of exactly ``chars`` characters."""
    chars: int


@dataclass(frozen=True)
class ListOf:
    item: Any
    max_items: int


# field name -> (kind, required, limit)
#   kind: str/int/float/bool, a tuple of allowed values, a nested spec (dict), B64, Hex, ListOf
#   limit: max length for str, (min, max) for numbers
Spec = Dict[str, Tuple[Any, bool, Any]]

_HEX = re.compile(r"^[0-9a-f]*$")

NAME = (str, True, 32)
KEY = (B64(32), True, None)
NONCE = (B64(16), True, None)
SEQ = (int, True, (0, MAX_CLOCK))
CLOCK = (int, True, (0, MAX_CLOCK))
POS = (int, True, (0, MAX_POSITION_MS))
FILM: Spec = {"name": (str, True, 255), "size": (int, True, (0, 1 << 50)),
              "dur": (int, False, (0, MAX_POSITION_MS))}
RULES: Spec = {"host_only": (bool, True, None), "cd_pp": (float, False, (0, 3600)),
               "cd_sync": (float, False, (0, 3600))}
MEMBER: Spec = {"id": KEY, "dh": KEY, "name": NAME, "host": (bool, False, None)}
VERSION_FIELDS: Spec = {"app": (str, True, 20), "v": (int, True, (0, 10_000))}

LOBBY: Dict[str, Spec] = {
    "hello": {},
    "card": {"name": NAME, "dh": KEY, **VERSION_FIELDS, "challenge": NONCE,
             "people": (int, True, (0, MAX_PEOPLE)), "max": (int, True, (1, MAX_PEOPLE)),
             "policy": (POLICIES, True, None)},
}

DIRECT: Dict[str, Spec] = {
    # asking to join and the host's answers (sent through the room's channel)
    "knock": {"challenge": NONCE, "nonce": NONCE, "name": NAME, "dh": KEY, **VERSION_FIELDS},
    "welcome": {"nonce": NONCE, "epoch": (B64(8), True, None), "key": KEY, "no": SEQ,
                "members": (ListOf(MEMBER, MAX_PEOPLE), True, None), "room": NAME},
    "deny": {"nonce": NONCE, "reason": (("denied", "full", "friends_only", "version", "busy"), True, None)},
    "rekey": {"epoch": (B64(8), True, None), "key": KEY, "no": SEQ,
              "members": (ListOf(MEMBER, MAX_PEOPLE), True, None)},
    "kicked": {"ban": (bool, False, None)},
    "moved": {"code": (str, True, 600)},
    # friends, asked in a room
    "friend_req": {"name": NAME, "mailbox": (Hex(32), True, None)},
    "friend_ok": {"name": NAME, "mailbox": (Hex(32), True, None), "dh": (B64(32), False, None)},
    "friend_no": {},
    # friends' mailboxes
    "hosting": {"code": (str, True, 600), "room": NAME, "ts": (float, True, (0, 1e13))},
    "hosting_end": {"ts": (float, True, (0, 1e13))},
    "who_hosts": {"ts": (float, True, (0, 1e13))},
    "unfriend": {"ts": (float, True, (0, 1e13))},
}

ROOM: Dict[str, Spec] = {
    # membership (host only)
    "members": {"no": SEQ, "list": (ListOf(MEMBER, MAX_PEOPLE), True, None)},
    "close": {},
    "bye": {},
    "ping": {"id": (float, True, (0, 1e13))},
    "pong": {"id": (float, True, (0, 1e13)), "to": (B64(8), True, None), "at": CLOCK},
    # watching together
    "profile": {"name": NAME, "avatar": (str, False, MAX_AVATAR_CHARS)},
    "state": {"pos": POS, "playing": (bool, True, None), "at": CLOCK, "ready": (bool, False, None),
              "film": (FILM, False, None), "rules": (RULES, True, None)},
    "status": {"pos": POS, "playing": (bool, True, None), "at": CLOCK, "separate": (bool, False, None),
               "ready": (bool, False, None), "film": (FILM, False, None)},
    "req": {"action": (("play", "pause", "seek", "syncup"), True, None),
            "pos": (int, False, (0, MAX_POSITION_MS))},
    "cmd": {"action": (("play", "pause", "seek"), True, None), "pos": POS, "playing": (bool, True, None),
            "at": CLOCK, "by": (str, False, 32), "by_id": (B64(8), False, None)},
    "reject": {"to": (B64(8), True, None), "action": (str, True, 20),
               "reason": (("cooldown", "host_only", "busy"), True, None), "wait": (float, False, (0, 3600))},
    "sync_prepare": {"id": (int, True, (0, 1 << 31)), "pos": POS, "by": (str, False, 32)},
    "sync_ready": {"id": (int, True, (0, 1 << 31))},
    "sync_go": {"id": (int, True, (0, 1 << 31)), "start": CLOCK},
    "sync_abort": {"id": (int, True, (0, 1 << 31))},
}

COMMON: Spec = {"s": (Hex(16), True, None), "n": SEQ}


def _check_value(name: str, value: Any, kind: Any, limit: Any) -> Any:
    if isinstance(kind, dict):  # nested object
        if not isinstance(value, dict):
            raise BadMessage(f"{name}: expected object")
        return check_fields(value, kind)
    if isinstance(kind, ListOf):
        if not isinstance(value, list) or len(value) > kind.max_items:
            raise BadMessage(f"{name}: expected a list of at most {kind.max_items}")
        return [_check_value(f"{name}[{i}]", item, kind.item, None) for i, item in enumerate(value)]
    if isinstance(kind, B64):
        if not isinstance(value, str) or len(value) > (kind.size + 2) // 3 * 4:
            raise BadMessage(f"{name}: expected base64")
        try:
            data = base64.b64decode(value, validate=True)
        except (binascii.Error, ValueError):
            raise BadMessage(f"{name}: not valid base64") from None
        if len(data) != kind.size:
            raise BadMessage(f"{name}: expected {kind.size} bytes")
        return data
    if isinstance(kind, Hex):
        if not isinstance(value, str) or len(value) != kind.chars or not _HEX.match(value):
            raise BadMessage(f"{name}: expected {kind.chars} hex characters")
        return value
    if isinstance(kind, tuple):  # one of fixed strings
        if value not in kind:
            raise BadMessage(f"{name}: unexpected value")
        return value
    if kind is bool:
        if not isinstance(value, bool):
            raise BadMessage(f"{name}: expected true/false")
        return value
    if kind in (int, float):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise BadMessage(f"{name}: expected number")
        if isinstance(value, float) and not math.isfinite(value):
            raise BadMessage(f"{name}: not a finite number")
        if kind is int:
            if isinstance(value, float):
                if not value.is_integer():
                    raise BadMessage(f"{name}: expected whole number")
                value = int(value)
        else:
            value = float(value)
        low, high = limit
        if not low <= value <= high:
            raise BadMessage(f"{name}: out of range")
        return value
    if kind is str:
        if not isinstance(value, str):
            raise BadMessage(f"{name}: expected text")
        if len(value) > limit:
            raise BadMessage(f"{name}: too long")
        return "".join(ch for ch in value if ch >= " " or ch == "\t").replace("\x7f", "")
    raise BadMessage(f"{name}: bad spec")  # pragma: no cover


def check_fields(raw: Dict[str, Any], spec: Spec) -> Dict[str, Any]:
    clean: Dict[str, Any] = {}
    for name, (kind, required, limit) in spec.items():
        if name not in raw or raw[name] is None:
            if required:
                raise BadMessage(f"missing {name}")
            continue
        clean[name] = _check_value(name, raw[name], kind, limit)
    return clean


def validate(table: Dict[str, Spec], body: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """A received body -> its checked copy, or None for a type this version doesn't know.

    Raises BadMessage for anything malformed (including a type that belongs to another table).
    """
    if not isinstance(body, dict) or not isinstance(body.get("t"), str):
        raise BadMessage("not a message object")
    kind = body["t"]
    spec = table.get(kind)
    if spec is None:
        if any(kind in other for other in (LOBBY, DIRECT, ROOM)):
            raise BadMessage(f"{kind} doesn't belong in this kind of envelope")
        return None
    clean = check_fields(body, COMMON)
    clean.update(check_fields(body, spec))
    clean["t"] = kind
    return clean


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")
