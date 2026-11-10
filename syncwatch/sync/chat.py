"""Room chat and emoji reactions (0.5): short messages and a few emoji for everyone in the room.

Nothing is kept: a message is shown when it comes (in the sidebar, and over the video while the
sidebar is hidden) and is gone with the room. Messages travel like everything else in a room
(encrypted, through the relay). Every app takes only so much from one person (a flood is
dropped) and shows text as it is (never HTML; control and invisible characters are removed by
net/protocol.py). Reactions are one of a fixed set of emoji (protocol.REACTIONS), so nothing else
can be drawn over anyone's video.
"""
from __future__ import annotations

import collections
from typing import Deque, Dict, Hashable

from ..net.protocol import CHAT_TEXT, REACTIONS

SEND_RATE, SEND_WINDOW_S = 5, 5.0          # messages I may send in this time (more: "slow down")
TAKE_RATE, TAKE_WINDOW_S = 6, 5.0          # messages taken from one person in this time
REACT_RATE, REACT_WINDOW_S = 6, 3.0        # reactions sent / taken per person in this time


class Limiter:
    """At most ``rate`` events per ``window`` seconds per person."""

    def __init__(self, rate: int, window: float):
        self.rate, self.window = rate, window
        self._times: Dict[Hashable, Deque[float]] = {}

    def allow(self, who: Hashable, now: float) -> bool:
        times = self._times.setdefault(who, collections.deque(maxlen=self.rate))
        if len(times) == self.rate and now - times[0] < self.window:
            return False
        times.append(now)
        return True

    def clear(self) -> None:
        self._times.clear()


def clean(text: str) -> str:
    """One line, no runs of spaces, at most CHAT_TEXT characters."""
    return " ".join("".join(ch for ch in (text or "") if ch >= " " or ch == "\t").split())[:CHAT_TEXT]


class RoomChat:
    """What the controller hands "chat" and "react" messages to; the window calls say() and react()."""

    def __init__(self, controller) -> None:
        self.c = controller
        self._send = Limiter(SEND_RATE, SEND_WINDOW_S)
        self._take = Limiter(TAKE_RATE, TAKE_WINDOW_S)
        self._send_react = Limiter(REACT_RATE, REACT_WINDOW_S)
        self._take_react = Limiter(REACT_RATE + 2, REACT_WINDOW_S)
        self._warned_at = -1e9

    def say(self, text: str) -> bool:
        """Send a message to the room. False if there's nothing to send, no room, or too many at once."""
        text = clean(text)
        if not text or not self.c.connected:
            return False
        now = self.c.clock()
        if not self._send.allow("me", now):
            if now - self._warned_at > SEND_WINDOW_S:
                self._warned_at = now
                self.c.ui.activity(f"Slow down a little: at most {SEND_RATE} messages in {SEND_WINDOW_S:.0f} s",
                                   "warn")
            return False
        self.c.session.send_room("chat", text=text)
        self.c.ui.chat_message(self.c.name, text, True)
        return True

    def react(self, emoji: str) -> bool:
        """Float an emoji over everyone's video (alone: only over mine)."""
        if emoji not in REACTIONS or not self._send_react.allow("me", self.c.clock()):
            return False
        self.c.ui.reaction(self.c.name, emoji, True)
        if self.c.connected:
            self.c.session.send_room("react", e=emoji)
        return True

    def on_chat(self, peer, body) -> None:
        if not self._take.allow(peer.id, self.c.clock()):
            return    # a flood: dropped
        text = clean(body["text"])
        if text:
            self.c.ui.chat_message(peer.name, text, False)

    def on_react(self, peer, body) -> None:
        if self._take_react.allow(peer.id, self.c.clock()):
            self.c.ui.reaction(peer.name, body["e"], False)

    def reset(self) -> None:
        for limiter in (self._send, self._take, self._send_react, self._take_react):
            limiter.clear()
