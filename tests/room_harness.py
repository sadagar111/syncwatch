"""A room full of SyncWatch apps without windows: for tests, the GUI checks and the bots.

    room = Room()                      # starts a relay on this PC (tools/local_relay.py),
                                       # or uses $SYNCWATCH_TEST_RELAY (see ExternalRelay)
    alex = room.person("Alex")
    marek = room.person("Marek")
    alex.c.host_room()
    marek.c.join_room(alex.code())
    room.pump(until=lambda: alex.c.session.waiting)
    alex.c.approve_all()

Each person is a real Controller with its own identity, profile folder and
relay connection; only the video player and the window are stand-ins.
"""
from __future__ import annotations

import os
import queue
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Callable, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from syncwatch.room.context import make_rooms  # noqa: E402
from syncwatch.settings import Settings  # noqa: E402
from syncwatch.sync.controller import Controller  # noqa: E402
from tools.local_relay import LocalRelay  # noqa: E402


class FakePlayer:
    """Plays an imaginary film in real time. ``seek_delay`` = how long a jump takes to show a picture."""

    def __init__(self, seek_delay: float = 0.05, duration: int = 7_200_000, media: bool = True):
        self.base, self.base_time, self.ready_at = 0.0, time.monotonic(), 0.0
        self.playing = False
        self.seek_delay = seek_delay
        self.dur = duration
        self.seeks = 0
        self.media = media
        self.rate = 1.0          # 1.01 = runs 1 % fast (a "drifting" PC)

    def position(self):
        if not self.playing:
            return self.base
        now = time.monotonic()
        return self.base + max(0.0, now - max(self.base_time, self.ready_at)) * 1000 * self.rate

    def duration(self):
        return self.dur

    def is_playing(self):
        return self.playing

    def is_ready(self):
        return time.monotonic() >= self.ready_at

    def has_media(self):
        return self.media

    def play(self):
        if not self.playing:
            self.base, self.base_time, self.playing = self.position(), time.monotonic(), True

    def pause(self):
        self.base, self.playing = self.position(), False

    def seek(self, ms):
        self.base, self.base_time = float(ms), time.monotonic()
        self.ready_at = time.monotonic() + self.seek_delay
        self.seeks += 1

    def drift(self, ms):
        self.base += ms


class RecorderUI:
    def __init__(self, name: str = "", echo: bool = False):
        self.name = name
        self.echo = echo
        self.events: list = []
        self.countdowns: list = []
        self.requests: list = []

    def _add(self, event):
        self.events.append(event)
        if self.echo:
            print(f"[{self.name}] {event[0]}: {event[1]}", flush=True)

    def status(self, text, connected):
        self._add(("status", text))

    def activity(self, text, kind="info"):
        self._add(("activity", text))

    def problem(self, code, technical="", **params):
        self._add(("problem", code))

    def countdown(self, ms):
        self.countdowns.append(ms)

    def join_request(self, knock):
        self.requests.append(knock)

    def changed(self):
        pass

    def problems(self):
        return [e[1] for e in self.events if e[0] == "problem"]

    def activities(self):
        return [e[1] for e in self.events if e[0] == "activity"]


class Person:
    def __init__(self, room: "Room", name: str, clock_offset: float = 0.0, seek_delay: float = 0.05,
                 media: bool = True, echo: bool = False, **settings):
        self.name = name
        self.room = room
        self.settings = Settings(None)
        defaults = {"profile.name": name, "room.relay": room.relay_url, "sync.countdown_s": 1,
                    "sync.seek_debounce_ms": 300}
        for key, value in {**defaults, **{k.replace("__", "."): v for k, v in settings.items()}}.items():
            assert self.settings.set(key, value), f"bad test setting {key}={value!r}"
        self.dir = room.data / name
        self.dir.mkdir(parents=True, exist_ok=True)
        self.rooms = make_rooms(self.dir, self.settings)
        self.inbox: "queue.Queue[dict]" = queue.Queue()
        self.player = FakePlayer(seek_delay=seek_delay, media=media)
        self.ui = RecorderUI(name, echo)
        self.c = Controller(self.player, self.ui, self.settings, self.inbox.put, self.rooms,
                            clock=lambda: time.monotonic() + clock_offset)
        self.c.set_profile(name, None)

    @property
    def id(self) -> bytes:
        return self.rooms.identity.public.key_id

    def code(self) -> str:
        return self.c.session.code.text()

    def pump(self) -> None:
        while True:
            try:
                event = self.inbox.get_nowait()
            except queue.Empty:
                break
            self.c.handle(event)
        self.c.tick()

    def peer(self, other: "Person"):
        return self.c.peers.get(other.id)


class ExternalRelay:
    """A relay that's already running elsewhere, named by SYNCWATCH_TEST_RELAY: for example the real
    relay code in Cloudflare's local runtime (``npx wrangler dev --port 8787`` in relay/), so the
    room tests check it too. Tests that need to restart the relay are skipped then."""

    def __init__(self, url: str):
        self.url = url.rstrip("/")

    def stop(self) -> None:
        pass


def relay_for_tests():
    url = os.environ.get("SYNCWATCH_TEST_RELAY", "")
    return ExternalRelay(url) if url else LocalRelay().start()


class Room:
    def __init__(self, relay: Optional[LocalRelay] = None):
        self.relay = relay or relay_for_tests()
        self._own_relay = relay is None
        self.relay_url = self.relay.url
        self.data = Path(tempfile.mkdtemp(prefix="sw-room-"))
        self.people: List[Person] = []

    def person(self, name: str, **kwargs) -> Person:
        person = Person(self, name, **kwargs)
        self.people.append(person)
        return person

    def pump(self, seconds: float = 0.0, until: Optional[Callable[[], bool]] = None, timeout: float = 15.0) -> bool:
        """Run everyone for ``seconds``, or until ``until()`` is true (False if it never was)."""
        end = time.monotonic() + (seconds if until is None else timeout)
        while True:  # at least once, even for pump(0)
            for person in list(self.people):
                person.pump()
            if until is not None and until():
                return True
            if time.monotonic() >= end:
                return until is None
            time.sleep(0.01)

    def let_in(self, host: Person, *guests: Person, timeout: float = 15.0) -> None:
        """Guests join the host's room and the host approves them."""
        if not host.c.session.is_host:
            assert host.c.host_room(), host.ui.events
            assert self.pump(until=lambda: host.c.session.channel_state == "connected"), host.ui.events
        for guest in guests:
            assert guest.c.join_room(host.code()), guest.ui.events
        assert self.pump(until=lambda: len(host.c.session.waiting) >= len(guests), timeout=timeout), \
            [g.ui.events for g in guests]
        host.c.approve_all()
        assert self.pump(until=lambda: all(g.c.connected for g in guests)
                         and len(host.c.peers) >= len(guests), timeout=timeout), [g.ui.events for g in guests]

    def close(self) -> None:
        for person in self.people:
            try:
                person.c.shutdown()
            except Exception:
                pass
        if self._own_relay:
            self.relay.stop()
        shutil.rmtree(self.data, ignore_errors=True)
