"""Sizes and times for people: "4.21 GB", "about 2 min 05 s left", and a smoothed ETA."""
from __future__ import annotations

import time
from typing import Callable, Optional, Tuple


def human_size(size: float) -> str:
    return f"{size / 1e6:.1f} MB" if size < 1e9 else f"{size / 1e9:.2f} GB"


def human_time(seconds) -> str:
    """Time left: 'about 40 s left', 'about 2 min 05 s left', or 'estimating…'."""
    if seconds is None:
        return "estimating…"
    seconds = int(seconds + 0.5)
    if seconds < 60:
        return f"about {seconds} s left"
    if seconds < 3600:
        return f"about {seconds // 60} min {seconds % 60:02d} s left"
    return f"about {seconds // 3600} h {seconds % 3600 // 60:02d} min left"


class Eta:
    """Speed and time left of a long job (copying, converting), smoothed so they don't jump around.

    ``update(done)`` with the amount done so far (bytes, seconds of film, anything that grows
    to ``total``) returns (amount per second, seconds left). Both are None for the first
    ``warmup`` seconds, when there's too little to go on.
    """

    def __init__(self, total: float, warmup: float = 1.0, clock: Callable[[], float] = time.monotonic):
        self.total = max(0.0, float(total))
        self.warmup = warmup
        self.clock = clock
        self.started = clock()
        self._last_time = self.started
        self._last_done = 0.0
        self._rate: Optional[float] = None

    def update(self, done: float) -> Tuple[Optional[float], Optional[float]]:
        now = self.clock()
        elapsed = now - self._last_time
        if elapsed >= 0.5:
            rate = (done - self._last_done) / elapsed
            self._rate = rate if self._rate is None else 0.7 * self._rate + 0.3 * rate
            self._last_time, self._last_done = now, done
        if now - self.started < self.warmup or not self._rate or self._rate <= 0:
            return self._rate, None
        return self._rate, max(0.0, (self.total - done) / self._rate)
