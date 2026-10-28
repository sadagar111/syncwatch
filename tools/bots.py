"""Fake viewers for testing a room with many people, without inviting anyone.

    python tools/bots.py <room code> [--count 9] [--lag-ms 0] [--drift] [--chaos]
    (or tools\\bots.bat <room code> ...)

Each bot is a real SyncWatch (the same code as the app, with its own identity
and relay connection) watching an imaginary film. They join your room like
people do, so you let them in ("Let all 9 in"), and then they follow along:
you see them in the People list, with their gaps, and on the seek bar.

    --count N     how many bots (default 3, at most 19)
    --lag-ms N    each jump takes N ms before the picture is ready (a slow PC)
    --drift       each bot's film runs a little fast or slow, so gaps grow over time
    --chaos       now and then a random bot presses play, pause, a jump or Sync up
    --names a,b   names to use (default Bot 1, Bot 2, ...)

Bots keep their identities between runs (in your SyncWatch data folder, under
"bots"), so friends lists and blocking work on them like on people.
Ctrl+C ends it; the bots say goodbye and leave.
"""
from __future__ import annotations

import argparse
import queue
import random
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from syncwatch.paths import data_root  # noqa: E402
from syncwatch.room.codes import CodeError, parse  # noqa: E402
from syncwatch.room.context import make_rooms  # noqa: E402
from syncwatch.settings import Settings  # noqa: E402
from syncwatch.sync.controller import Controller  # noqa: E402

MAX_BOTS = 19


class FakePlayer:
    """An imaginary film playing in real time (optionally a bit fast or slow)."""

    def __init__(self, seek_delay: float, rate: float, duration: int = 2 * 3600 * 1000):
        self.base, self.base_time, self.ready_at = 0.0, time.monotonic(), 0.0
        self.playing = False
        self.seek_delay = seek_delay
        self.rate = rate
        self.dur = duration

    def position(self):
        if not self.playing:
            return self.base
        elapsed = max(0.0, time.monotonic() - max(self.base_time, self.ready_at))
        return min(self.dur, self.base + elapsed * 1000 * self.rate)

    def duration(self):
        return self.dur

    def is_playing(self):
        return self.playing

    def is_ready(self):
        return time.monotonic() >= self.ready_at

    def has_media(self):
        return True

    def play(self):
        if not self.playing:
            self.base, self.base_time, self.playing = self.position(), time.monotonic(), True

    def pause(self):
        self.base, self.playing = self.position(), False

    def seek(self, ms):
        self.base, self.base_time = float(ms), time.monotonic()
        self.ready_at = time.monotonic() + self.seek_delay


class PrintUI:
    def __init__(self, name: str):
        self.name = name

    def _say(self, text: str) -> None:
        print(f"{time.strftime('%H:%M:%S')}  [{self.name}] {text}", flush=True)

    def status(self, text, connected):
        self._say(text)

    def activity(self, text, kind="info"):
        self._say(text)

    def problem(self, code, technical="", **params):
        self._say(f"problem {code} {technical}".strip())

    def countdown(self, ms):
        self._say(f"countdown {ms / 1000:.1f} s")

    def changed(self):
        pass


class Bot:
    def __init__(self, name: str, relay: str, lag_ms: int, drift: bool):
        self.name = name
        self.settings = Settings(None)
        self.settings.set("profile.name", name)
        if relay:
            self.settings.set("room.relay", relay)
        folder = data_root() / "bots" / name.replace(" ", "_")
        folder.mkdir(parents=True, exist_ok=True)
        self.inbox: "queue.Queue[dict]" = queue.Queue()
        rate = 1.0 + (random.uniform(-0.004, 0.004) if drift else 0.0)
        self.player = FakePlayer(lag_ms / 1000.0, rate)
        self.c = Controller(self.player, PrintUI(name), self.settings, self.inbox.put,
                            make_rooms(folder, self.settings))
        self.c.set_profile(name, None)

    def pump(self) -> None:
        while True:
            try:
                event = self.inbox.get_nowait()
            except queue.Empty:
                break
            self.c.handle(event)
        self.c.tick()


def chaos(bots) -> None:
    bot = random.choice([b for b in bots if b.c.connected] or bots)
    action = random.choice(["play", "pause", "seek", "seek", "syncup"])
    print(f"{time.strftime('%H:%M:%S')}  [{bot.name}] (chaos) presses {action}", flush=True)
    if action == "play":
        bot.c.play()
    elif action == "pause":
        bot.c.pause()
    elif action == "seek":
        bot.c.seek(bot.player.position() + random.choice([-10_000, 10_000, 60_000]))
    else:
        bot.c.sync_up()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Fake viewers for testing a room.")
    parser.add_argument("code", help="the room code (Copy code in SyncWatch)")
    parser.add_argument("--count", type=int, default=3)
    parser.add_argument("--lag-ms", type=int, default=0)
    parser.add_argument("--drift", action="store_true")
    parser.add_argument("--chaos", action="store_true")
    parser.add_argument("--names", default="")
    parser.add_argument("--relay", default="", help="relay address (default: the one SyncWatch uses)")
    args = parser.parse_args(argv)
    try:
        code = parse(args.code)
    except CodeError as exc:
        print(f"That room code doesn't work: {exc}")
        return 2
    count = max(1, min(MAX_BOTS, args.count))
    names = [n.strip()[:32] for n in args.names.split(",") if n.strip()] or [f"Bot {i + 1}" for i in range(count)]
    bots = [Bot(name, args.relay, args.lag_ms, args.drift) for name in names[:count]]
    for bot in bots:
        if not bot.c.join_code(code):
            print("Couldn't start joining (is a relay set up? see docs/GIT_GUIDE.md lesson 5)")
            return 1
    print(f"{len(bots)} bots are asking to join. Let them in from SyncWatch (\"Let all {len(bots)} in\"). "
          "Ctrl+C ends.", flush=True)
    next_chaos = time.monotonic() + random.uniform(10, 25)
    try:
        while True:
            for bot in bots:
                bot.pump()
            if args.chaos and time.monotonic() >= next_chaos:
                next_chaos = time.monotonic() + random.uniform(10, 30)
                chaos(bots)
            time.sleep(0.02)
    except KeyboardInterrupt:
        print("Leaving…", flush=True)
    for bot in bots:
        bot.c.shutdown()
    time.sleep(0.5)
    return 0


if __name__ == "__main__":
    sys.exit(main())
