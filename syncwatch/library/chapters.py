"""Chapters a film has (from mpv): is the one playing now an intro, an opening or a recap? No Qt here.

    skippable_at([(0, "Intro"), (90_000, "Episode")], 30_000, 1_500_000) -> (90_000, "Intro")

Only by the chapter's title (in English, as most releases name them), and only chapters of at most
five minutes: a long "Opening" is more likely the film itself than its opening titles.
"""
from __future__ import annotations

import re
from typing import List, Optional, Sequence, Tuple

SKIPPABLE = re.compile(r"(?i)\b(intro|introduction|opening|op|recap|previously|theme song|title sequence)\b")
MAX_LENGTH_MS = 5 * 60_000


def skippable_at(chapters: Sequence[Tuple[int, str]], position_ms: float, duration_ms: int) -> Optional[Tuple[int, str]]:
    """(where the chapter playing now ends, its title) if it's an intro, an opening or a recap; else None."""
    ordered: List[Tuple[int, str]] = sorted(chapters)
    for index, (start, title) in enumerate(ordered):
        end = ordered[index + 1][0] if index + 1 < len(ordered) else int(duration_ms)
        if start <= position_ms < end - 1000 and SKIPPABLE.search(title or "") and 0 < end - start <= MAX_LENGTH_MS:
            return end, title
    return None
