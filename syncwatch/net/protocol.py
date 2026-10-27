"""The messages two SyncWatch apps send each other, and strict checking of them.

Every message is one line of JSON: ``{"t": "<type>", ...fields}``. Anything
received is checked against the table below before the app acts on it: wrong
types, missing fields, absurd numbers, over-long text and unknown message
types are all rejected. A friend's app (or anything pretending to be one)
therefore can't crash yours or make it do something unexpected by sending
odd data.
"""
from __future__ import annotations

import json
import math
from typing import Any, Dict, Optional, Tuple

MAX_LINE_BYTES = 256 * 1024
MAX_POSITION_MS = 48 * 3600 * 1000          # nothing is longer than two days
MAX_AVATAR_CHARS = 120_000                   # base64 of a ~90 KB image

# field name -> (allowed types, required, limit)
#   limit: max length for str, (min, max) for numbers, allowed values for "choice" tuples
Spec = Dict[str, Tuple[Any, bool, Any]]
_FILM: Spec = {"name": (str, True, 255), "size": (int, True, (0, 1 << 50)), "dur": (int, False, (0, MAX_POSITION_MS))}
_RULES: Spec = {"host_only": (bool, True, None), "cd_pp": (float, False, (0, 3600)),
                "cd_sync": (float, False, (0, 3600))}

MESSAGES: Dict[str, Spec] = {
    "hello": {"v": (int, True, (0, 10_000)), "app": (str, True, 20), "name": (str, True, 32),
              "avatar": (str, False, MAX_AVATAR_CHARS), "film": (_FILM, False, None)},
    "bye": {"reason": (str, True, 20), "app": (str, False, 20), "v": (int, False, (0, 10_000))},
    "ping": {"id": (float, True, (0, 1e12))},
    "pong": {"id": (float, True, (0, 1e12))},
    "profile": {"name": (str, True, 32), "avatar": (str, False, MAX_AVATAR_CHARS)},
    "film": {"film": (_FILM, False, None)},
    "state": {"pos": (int, True, (0, MAX_POSITION_MS)), "playing": (bool, True, None),
              "separate": (bool, False, None), "ready": (bool, False, None),
              "rules": (_RULES, False, None)},
    "req": {"action": (("play", "pause", "seek", "syncup"), True, None),
            "pos": (int, False, (0, MAX_POSITION_MS))},
    "cmd": {"action": (("play", "pause", "seek"), True, None), "pos": (int, True, (0, MAX_POSITION_MS)),
            "playing": (bool, False, None), "by": (str, False, 32)},
    "reject": {"action": (str, True, 20), "reason": (("cooldown", "host_only", "busy"), True, None),
               "wait": (float, False, (0, 3600))},
    "sync_prepare": {"id": (int, True, (0, 1 << 31)), "pos": (int, True, (0, MAX_POSITION_MS)),
                     "by": (str, False, 32)},
    "sync_ready": {"id": (int, True, (0, 1 << 31))},
    "sync_go": {"id": (int, True, (0, 1 << 31)), "delay": (int, True, (0, 20_000))},
    "sync_abort": {"id": (int, True, (0, 1 << 31))},
}


class BadMessage(ValueError):
    pass


def _check_value(name: str, value: Any, kind: Any, limit: Any) -> Any:
    if isinstance(kind, dict):  # nested object
        if not isinstance(value, dict):
            raise BadMessage(f"{name}: expected object")
        return _check_fields(value, kind)
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
        return value.replace("\x00", "")
    raise BadMessage(f"{name}: bad spec")  # pragma: no cover


def _check_fields(raw: Dict[str, Any], spec: Spec) -> Dict[str, Any]:
    clean: Dict[str, Any] = {}
    for name, (kind, required, limit) in spec.items():
        if name not in raw or raw[name] is None:
            if required:
                raise BadMessage(f"missing {name}")
            continue
        clean[name] = _check_value(name, raw[name], kind, limit)
    return clean


def decode(line: bytes) -> Optional[Dict[str, Any]]:
    """Bytes of one line -> checked message, or None for an unknown (newer) message type.

    Raises BadMessage for anything malformed.
    """
    if len(line) > MAX_LINE_BYTES:
        raise BadMessage("line too long")
    try:
        raw = json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise BadMessage(f"not JSON: {exc}") from exc
    if not isinstance(raw, dict) or not isinstance(raw.get("t"), str):
        raise BadMessage("not a message object")
    spec = MESSAGES.get(raw["t"])
    if spec is None:
        return None
    clean = _check_fields(raw, spec)
    clean["t"] = raw["t"]
    return clean


def encode(message: Dict[str, Any]) -> bytes:
    data = (json.dumps(message, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")
    if len(data) > MAX_LINE_BYTES:
        raise BadMessage("message too large to send")
    return data
