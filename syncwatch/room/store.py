"""What SyncWatch remembers about rooms and people, in your profile folder (``rooms.json``).

* your room's secret (so your room code stays the same until you press New code),
* your friends (their keys, name and mailbox),
* people you blocked,
* who was in your room recently (they get back in without asking after a
  short disconnect or when you restart SyncWatch; forgotten after 10 minutes).

Saved atomically (write a temp file, then rename), so a crash can't leave half a file.
A damaged file is kept aside and SyncWatch starts fresh: your room gets a new
code and friends have to be added again, but nothing crashes.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

from .codes import RoomCode
from .identity import Identity, PublicIdentity

log = logging.getLogger("syncwatch.rooms")

FORMAT = 1
RECENT_S = 10 * 60
MAX_FRIENDS = 200
MAX_BLOCKED = 500


@dataclass
class Friend:
    key: PublicIdentity
    name: str
    mailbox: str            # their mailbox channel (32 hex)
    added: float

    def to_json(self) -> dict:
        return {**self.key.to_json(), "name": self.name, "mailbox": self.mailbox, "added": self.added}

    @classmethod
    def from_json(cls, data: dict) -> "Friend":
        mailbox = str(data["mailbox"])
        if len(mailbox) != 32 or any(c not in "0123456789abcdef" for c in mailbox):
            raise ValueError("bad mailbox")
        return cls(PublicIdentity.from_json(data), str(data["name"])[:32], mailbox, float(data.get("added", 0)))


@dataclass
class Blocked:
    key: PublicIdentity
    name: str
    at: float

    def to_json(self) -> dict:
        return {**self.key.to_json(), "name": self.name, "at": self.at}

    @classmethod
    def from_json(cls, data: dict) -> "Blocked":
        return cls(PublicIdentity.from_json(data), str(data["name"])[:32], float(data.get("at", 0)))


class RoomStore:
    def __init__(self, path: Path, wall=time.time):
        self.path = path
        self.wall = wall
        self.room_secret: Optional[bytes] = None
        self.friends: Dict[bytes, Friend] = {}
        self.blocked: Dict[bytes, Blocked] = {}
        self.recent: Dict[bytes, float] = {}   # full fingerprint -> last time in my room (wall clock)
        self.problem: Optional[str] = None
        self.load()

    # ------------------------------------------------------------------ files
    def load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if data.get("format") != FORMAT:
                raise ValueError(f"unknown format {data.get('format')!r}")
            secret = data.get("room_secret")
            self.room_secret = base64.b64decode(secret, validate=True) if secret else None
            if self.room_secret is not None and len(self.room_secret) != 16:
                raise ValueError("bad room secret")
            for item in data.get("friends", [])[:MAX_FRIENDS]:
                friend = Friend.from_json(item)
                self.friends[friend.key.key_id] = friend
            for item in data.get("blocked", [])[:MAX_BLOCKED]:
                blocked = Blocked.from_json(item)
                self.blocked[blocked.key.key_id] = blocked
            for key_hex, when in dict(data.get("recent", {})).items():
                fingerprint = bytes.fromhex(key_hex)
                if len(fingerprint) == 32:
                    self.recent[fingerprint] = float(when)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            self.problem = f"{type(exc).__name__}: {exc}"
            damaged = self.path.with_name(f"{self.path.name}.damaged-{int(time.time())}")
            try:
                os.replace(self.path, damaged)
            except OSError:
                pass
            log.error("rooms file damaged (%s); starting fresh, old file kept as %s", self.problem, damaged.name)
            self.room_secret, self.friends, self.blocked, self.recent = None, {}, {}, {}

    def save(self) -> bool:
        data = {
            "format": FORMAT,
            "room_secret": base64.b64encode(self.room_secret).decode("ascii") if self.room_secret else None,
            "friends": [f.to_json() for f in self.friends.values()],
            "blocked": [b.to_json() for b in self.blocked.values()],
            "recent": {k.hex(): v for k, v in self.recent.items()},
            "_note": "SyncWatch rooms: your room code's secret, friends and blocked people. Keep it private.",
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(self.path.name + ".tmp")
            tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self.path)
            return True
        except OSError as exc:
            log.error("couldn't save rooms file: %s", exc)
            return False

    # ------------------------------------------------------------------ my room
    def my_code(self, me: Identity, relay: str = "") -> RoomCode:
        if self.room_secret is None:
            self.room_secret = os.urandom(16)
            self.save()
        return RoomCode(self.room_secret, me.public.key_id, relay)

    def new_code(self, me: Identity, relay: str = "") -> RoomCode:
        self.room_secret = os.urandom(16)
        self.save()
        return RoomCode(self.room_secret, me.public.key_id, relay)

    # ------------------------------------------------------------------ people
    def is_friend(self, key: PublicIdentity) -> bool:
        friend = self.friends.get(key.key_id)
        return friend is not None and friend.key == key

    def add_friend(self, key: PublicIdentity, name: str, mailbox: str) -> Friend:
        friend = Friend(key, name[:32], mailbox, self.wall())
        self.friends[key.key_id] = friend
        self.blocked.pop(key.key_id, None)
        self.save()
        return friend

    def remove_friend(self, key_id: bytes) -> Optional[Friend]:
        friend = self.friends.pop(key_id, None)
        if friend is not None:
            self.save()
        return friend

    def is_blocked(self, key: PublicIdentity) -> bool:
        return key.key_id in self.blocked

    def block(self, key: PublicIdentity, name: str) -> None:
        self.blocked[key.key_id] = Blocked(key, name[:32], self.wall())
        self.friends.pop(key.key_id, None)
        self.forget_recent(key.key_id)
        self.save()

    def unblock(self, key_id: bytes) -> None:
        if self.blocked.pop(key_id, None) is not None:
            self.save()

    def remember_in_room(self, keys: List[PublicIdentity]) -> None:
        now = self.wall()
        for key in keys:
            self.recent[key.fingerprint] = now   # the full fingerprint: a lookalike key id doesn't count
        self._forget_old(now)

    def was_recently_in_room(self, key: PublicIdentity) -> bool:
        when = self.recent.get(key.fingerprint)
        return when is not None and self.wall() - when < RECENT_S

    def forget_recent(self, key_id: Optional[bytes] = None) -> None:
        if key_id is None:
            self.recent.clear()
        else:
            for fingerprint in [f for f in self.recent if f[:8] == key_id]:
                del self.recent[fingerprint]

    def _forget_old(self, now: float) -> None:
        for fingerprint in [k for k, when in self.recent.items() if now - when >= RECENT_S]:
            del self.recent[fingerprint]
