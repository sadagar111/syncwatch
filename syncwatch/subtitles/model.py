"""Subtitles in memory: a list of cues and fast lookup of what to show when.

A cue's text uses a tiny markup that the overlay draws: plain text with
newlines, plus <i>…</i>, <b>…</b> and <u>…</u>. Every parser converts its own
format (SRT, VTT, ASS, tracks inside MKV) into this.

Delay and stretch are applied at lookup time, so changing them is instant:
    shown on screen at   start * stretch + delay   …   end * stretch + delay
Stretch fixes subtitles that start in sync but drift further off over time,
because they were timed for a version of the film with another frame rate
(e.g. made for 25 fps but the film is 23.976 fps: stretch = 25 / 23.976).
"""
from __future__ import annotations

import bisect
import threading
from dataclasses import dataclass
from typing import Iterable, List, Optional

# Common frame-rate fixes: (label, stretch factor)
STRETCH_PRESETS = [
    ("None", 1.0),
    ("Subtitles for 25 fps, film is 23.976 fps", 25 / 23.976),
    ("Subtitles for 23.976 fps, film is 25 fps", 23.976 / 25),
    ("Subtitles for 25 fps, film is 24 fps", 25 / 24),
    ("Subtitles for 24 fps, film is 25 fps", 24 / 25),
    ("Subtitles for 24 fps, film is 23.976 fps", 24 / 23.976),
    ("Subtitles for 23.976 fps, film is 24 fps", 23.976 / 24),
]

MAX_CUE_MS = 60_000  # a cue longer than a minute is treated as broken and clipped


@dataclass(frozen=True)
class Cue:
    start: int          # ms
    end: int            # ms
    text: str           # mini markup, see module docstring
    top: bool = False   # shown at the top of the screen (e.g. ASS {\an8})


class SubtitleTrack:
    """Cues sorted by start time. Cues may be added while it's in use (MKV reading)."""

    def __init__(self, cues: Iterable[Cue] = (), label: str = "", language: str = "",
                 source: str = ""):
        self.label = label
        self.language = language
        self.source = source           # e.g. "file:C:/…/film.sk.srt" or "mkv:3"
        self.complete = True           # False while still being read from a film
        self._lock = threading.Lock()
        self._cues: List[Cue] = []
        self._starts: List[int] = []
        self._max_len = 0
        self.add(cues)

    def add(self, cues: Iterable[Cue]) -> None:
        with self._lock:
            for cue in cues:
                if cue.end <= cue.start or not cue.text.strip():
                    continue
                if cue.end - cue.start > MAX_CUE_MS:
                    cue = Cue(cue.start, cue.start + MAX_CUE_MS, cue.text, cue.top)
                if not self._starts or cue.start >= self._starts[-1]:
                    self._cues.append(cue)
                    self._starts.append(cue.start)
                else:
                    index = bisect.bisect_right(self._starts, cue.start)
                    self._cues.insert(index, cue)
                    self._starts.insert(index, cue.start)
                self._max_len = max(self._max_len, cue.end - cue.start)

    def __len__(self) -> int:
        with self._lock:
            return len(self._cues)

    def cues(self) -> List[Cue]:
        with self._lock:
            return list(self._cues)

    def active(self, position_ms: float, delay_ms: float = 0.0, stretch: float = 1.0) -> List[Cue]:
        """Cues to show at this moment of the film, in start order."""
        if stretch <= 0:
            stretch = 1.0
        t = (position_ms - delay_ms) / stretch   # the moment on the subtitles' own clock
        with self._lock:
            index = bisect.bisect_right(self._starts, t)
            found = []
            lowest_start = t - self._max_len
            while index > 0:
                index -= 1
                cue = self._cues[index]
                if cue.start < lowest_start:
                    break
                if cue.start <= t < cue.end:
                    found.append(cue)
            found.reverse()
            return found

    def first_start(self) -> Optional[int]:
        with self._lock:
            return self._starts[0] if self._starts else None
