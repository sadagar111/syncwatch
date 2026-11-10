"""Episodes of a show, from their file names, and which file comes next. No Qt in here.

    info("Breaking.Bad.S01E02.720p.BluRay.mkv")   -> Episode(show="breaking bad", season=1, number=2)
    info("The Office - 2x05 - Halloween.mkv")      -> Episode(show="the office", season=2, number=5)
    info("Inception.2010.1080p.mkv")               -> None (a film)
    next_after(Path(".../Breaking.Bad.S01E02.mkv")) -> Path(".../Breaking.Bad.S01E03.mkv")

The next episode is the one of the same show with the next higher season and number, in the same
folder or, after a season's last one, in a sibling folder ("Season 2" next to "Season 1").
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Optional, Tuple

from .films import VIDEO_EXTENSIONS

MAX_FILES = 2000          # a folder with more files than this isn't searched (a whole drive, say)

_SEP = r"[ ._\-]*"
_PATTERNS = (
    re.compile(rf"^(?P<show>.*?){_SEP}s(?P<season>\d{{1,2}}){_SEP}e(?P<number>\d{{1,3}})(?!\d)", re.IGNORECASE),
    re.compile(rf"^(?P<show>.*?){_SEP}(?<!\d)(?P<season>\d{{1,2}})x(?P<number>\d{{2,3}})(?!\d)", re.IGNORECASE),
    re.compile(rf"^(?P<show>.*?){_SEP}\b(?:episode|ep){_SEP}(?P<number>\d{{1,3}})(?!\d)", re.IGNORECASE),
)
_YEAR = re.compile(r"\(?\b(19|20)\d{2}\b\)?")
_NOT_WORDS = re.compile(r"[\W_]+")


@dataclass(frozen=True)
class Episode:
    show: str          # simplified: lower case, words separated by single spaces ("" if the name has none)
    season: int
    number: int

    @property
    def label(self) -> str:
        return f"S{self.season:02d}E{self.number:02d}"

    def same_show(self, other: "Episode") -> bool:
        return self.show == other.show


def _simplify(show: str) -> str:
    show = _YEAR.sub(" ", show)
    return " ".join(_NOT_WORDS.sub(" ", show).lower().split())


def info(name: str) -> Optional[Episode]:
    """The episode a file name stands for, or None (a film, or nothing recognisable)."""
    stem = Path(name).stem if Path(name).suffix.lower() in VIDEO_EXTENSIONS else name
    for pattern in _PATTERNS:
        match = pattern.search(stem)
        if match:
            season = int(match.groupdict().get("season") or 1)
            number = int(match.group("number"))
            if number == 0 and season == 0:
                return None
            return Episode(_simplify(match.group("show")), season, number)
    return None


def _videos(folder: Path) -> List[Path]:
    try:
        entries = []
        for index, path in enumerate(folder.iterdir()):
            if index >= MAX_FILES:
                return []
            if path.suffix.lower() in VIDEO_EXTENSIONS and path.is_file():
                entries.append(path)
        return entries
    except OSError:
        return []


def _later(me: Episode, paths: Iterable[Path]) -> Optional[Tuple[Tuple[int, int], Path]]:
    best = None
    for path in paths:
        episode = info(path.name)
        if episode is None or not episode.same_show(me) or (episode.season, episode.number) <= (me.season, me.number):
            continue
        key = (episode.season, episode.number)
        if best is None or key < best[0] or (key == best[0] and path.name < best[1].name):
            best = (key, path)
    return best


def next_after(path: Path) -> Optional[Path]:
    """The next episode of the same show: in the same folder, else in a folder next to it (the next
    season's). None if it's not an episode or nothing comes after it."""
    me = info(path.name)
    if me is None:
        return None
    here = _later(me, _videos(path.parent))
    if here is not None:
        return here[1]
    try:
        siblings = [p for p in path.parent.parent.iterdir() if p.is_dir() and p != path.parent][:MAX_FILES]
    except OSError:
        return None
    found = [_later(me, _videos(folder)) for folder in siblings]
    found = [f for f in found if f is not None]
    return min(found)[1] if found else None


def find(episode: Episode, near: Path) -> Optional[Path]:
    """That episode of that show in ``near``'s folder or the folders next to it (a member looking for
    the episode the host went on to). None if it isn't there."""
    folders = [near.parent]
    try:
        folders += [p for p in near.parent.parent.iterdir() if p.is_dir() and p != near.parent][:MAX_FILES]
    except OSError:
        pass
    for folder in folders:
        for path in sorted(_videos(folder)):
            if info(path.name) == episode:
                return path
    return None
