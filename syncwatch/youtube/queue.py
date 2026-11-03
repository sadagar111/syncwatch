"""The YouTube queue: what plays now and what comes next. No Qt in here.

In a room, the host's app holds the queue (like everything else that counts for the whole room):
members send requests ("add this", "skip"), the host's app checks them (rules, flood limits) and
sends the whole queue to everyone after every change. Alone, your own app is the host.

    queue = Queue()
    item = queue.add("dQw4w9WgXcQ", "Title", 212_000, "Alex")
    queue.advance()          # the first video becomes "now"; again: the next one
    message = queue.to_message()      # what the host sends ("queue" in net/protocol.py)
    other.load_message(message)       # what members do with it
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional

from .links import is_video_id

MAX_ITEMS = 50          # the whole queue must fit into one room message (64 KB)
MAX_TITLE = 120


@dataclass
class Item:
    key: int            # unique within this queue (the host counts up)
    id: str             # YouTube video ID
    title: str
    dur: int            # milliseconds, 0 = not known yet
    by: str             # who added it
    by_id: Optional[bytes] = None   # the host's app only: who added it, by key (for "remove my own")

    def to_message(self) -> Dict:
        return {"k": self.key, "yt": self.id, "title": self.title, "dur": self.dur, "by": self.by}

    @classmethod
    def from_message(cls, raw: Dict) -> "Item":
        return cls(int(raw["k"]), raw["yt"], raw["title"], int(raw.get("dur", 0)), raw.get("by", ""))


class QueueFull(Exception):
    pass


class Queue:
    def __init__(self) -> None:
        self.now: Optional[Item] = None      # playing (or last played)
        self.items: List[Item] = []          # up next, in order
        self.version = 0                     # counts every change (members ignore older messages)
        self._next_key = 1

    # ------------------------------------------------------------ changing (host)
    def add(self, video_id: str, title: str, dur: int, by: str) -> Item:
        if not is_video_id(video_id):
            raise ValueError("not a YouTube video ID")
        if len(self.items) >= MAX_ITEMS:
            raise QueueFull()
        item = Item(self._next_key, video_id, (title or video_id)[:MAX_TITLE], max(0, int(dur)), (by or "")[:32])
        self._next_key += 1
        self.items.append(item)
        self._changed()
        return item

    def contains(self, video_id: str) -> bool:
        return any(item.id == video_id for item in self.items)

    def find(self, key: int) -> Optional[Item]:
        return next((item for item in self.items if item.key == key), None)

    def remove(self, key: int) -> Optional[Item]:
        item = self.find(key)
        if item is not None:
            self.items.remove(item)
            self._changed()
        return item

    def move(self, key: int, index: int) -> bool:
        item = self.find(key)
        if item is None:
            return False
        self.items.remove(item)
        self.items.insert(max(0, min(len(self.items), int(index))), item)
        self._changed()
        return True

    def advance(self) -> Optional[Item]:
        """The next video becomes "now" (None: nothing left; "now" stays what played last)."""
        if not self.items:
            return None
        self.now = self.items.pop(0)
        self._changed()
        return self.now

    def play(self, key: int) -> Optional[Item]:
        """Jump the queue: this video plays now (it leaves "up next")."""
        item = self.find(key)
        if item is None:
            return None
        self.items.remove(item)
        self.now = item
        self._changed()
        return item

    def playing_elsewhere(self) -> None:
        """Something that isn't in the queue plays now (a film, a pasted link)."""
        if self.now is not None:
            self.now = None
            self._changed()

    def set_now(self, video_id: str, title: str, dur: int, by: str) -> Item:
        """A video played straight away (not from the queue) still shows as "now playing"."""
        self.now = Item(self._next_key, video_id, (title or video_id)[:MAX_TITLE], max(0, int(dur)), by[:32])
        self._next_key += 1
        self._changed()
        return self.now

    def update_info(self, video_id: str, title: str, dur: int) -> bool:
        """Fill in a title and length once the video was looked up."""
        changed = False
        for item in ([self.now] if self.now else []) + self.items:
            if item.id != video_id:
                continue
            if title and item.title != title[:MAX_TITLE]:
                item.title, changed = title[:MAX_TITLE], True
            if dur > 0 and item.dur != int(dur):        # 0 = not known: keep what we have
                item.dur, changed = int(dur), True
        if changed:
            self._changed()
        return changed

    def clear(self) -> None:
        if self.items or self.now:
            self.items, self.now = [], None
            self._changed()

    def _changed(self) -> None:
        self.version += 1

    # ------------------------------------------------------------ the room message
    def to_message(self) -> Dict:
        message = {"no": self.version, "items": [item.to_message() for item in self.items]}
        if self.now is not None:
            message["now"] = self.now.to_message()
        return message

    def load_message(self, message: Dict) -> bool:
        """A member takes over the host's queue (an already checked "queue" message). False if stale."""
        if message["no"] < self.version and message["no"] != 0:
            return False
        self.version = message["no"]
        self.items = [Item.from_message(raw) for raw in message["items"]][:MAX_ITEMS]
        self.now = Item.from_message(message["now"]) if message.get("now") else None
        return True

    def reset(self) -> None:
        """Leaving a room: a member's copy of the host's queue is gone (version counting starts over)."""
        self.items, self.now, self.version = [], None, 0

    # ------------------------------------------------------------ reading
    @property
    def next(self) -> Optional[Item]:
        return self.items[0] if self.items else None

    def total_ms(self) -> int:
        return sum(item.dur for item in self.items)
