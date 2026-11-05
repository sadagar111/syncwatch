"""The film vote in a room: everyone suggests films, everyone votes, the host closes it.

Held by the host's app, like the YouTube queue (youtube/queue.py): members send requests ("start a
vote", "suggest this film", "my vote", "I have these"), the host's app checks them and sends the
whole vote to everyone after every change. Votes are open (friends see who voted for what), one per
person, and can be changed until the host closes the vote. Everyone's app tells which suggested
films it has in its library (Settings > Discover), so the room sees which film is ready to play.
No Qt in here.

    vote = Vote()
    vote.start()
    matrix = vote.suggest("The Matrix", 1999, 603, "/poster.jpg", "Alex", by_id=b"alex")
    vote.cast(b"marek", "Marek", matrix.key)
    vote.mark_have(b"jana", "Jana", [matrix.key])
    winner = vote.close()            # most votes; a tie goes to the film more people have
    message = vote.to_message()      # "vote" in net/protocol.py
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Hashable, Iterable, List, Optional, Set

from .names import simplify

MAX_CHOICES = 12
PER_PERSON = 3          # films one person may suggest (so nobody fills the vote alone)
MAX_TITLE = 120


class VoteFull(Exception):
    """No room for more films (or this person suggested enough)."""


@dataclass
class Choice:
    key: int
    title: str
    year: int = 0
    tmdb: int = 0           # TMDB's number for the film, 0 if not known
    poster: str = ""        # TMDB image path, "" if none
    by: str = ""
    voters: List[str] = field(default_factory=list)
    have: List[str] = field(default_factory=list)
    by_id: Optional[Hashable] = None      # the host's app only: who suggested it

    @property
    def label(self) -> str:
        return f"{self.title} ({self.year})" if self.year else self.title

    def same_film(self, title: str, year: int, tmdb: int) -> bool:
        if tmdb and self.tmdb:
            return tmdb == self.tmdb
        return simplify(title) == simplify(self.title) and (not year or not self.year or year == self.year)

    def to_message(self) -> Dict[str, Any]:
        message = {"k": self.key, "title": self.title, "year": self.year, "tmdb": self.tmdb, "by": self.by,
                   "voters": list(self.voters), "have": list(self.have)}
        if self.poster:
            message["poster"] = self.poster
        return message

    @classmethod
    def from_message(cls, raw: Dict[str, Any]) -> "Choice":
        return cls(int(raw["k"]), raw["title"], int(raw.get("year", 0)), int(raw.get("tmdb", 0)),
                   raw.get("poster", ""), raw.get("by", ""), list(raw.get("voters", [])), list(raw.get("have", [])))


class Vote:
    def __init__(self) -> None:
        self.open = False
        self.choices: List[Choice] = []
        self.winner = 0                  # the winning film's key once the vote is closed
        self.version = 0                 # counts every change (members ignore older messages)
        self._next_key = 1
        self._ballots: Dict[Hashable, int] = {}      # voter -> key (the host's app)
        self._have: Dict[Hashable, Set[int]] = {}    # person -> keys of suggested films they have
        self._names: Dict[Hashable, str] = {}

    @property
    def active(self) -> bool:
        """Something to show: an open vote, or a closed one and its winner."""
        return self.open or self.find(self.winner) is not None

    def find(self, key: int) -> Optional[Choice]:
        return next((choice for choice in self.choices if choice.key == key), None) if key else None

    # ------------------------------------------------------------ changing (host)
    def start(self) -> None:
        self.open, self.choices, self.winner = True, [], 0
        self._ballots.clear()
        self._have.clear()
        self._changed()

    def suggest(self, title: str, year: int, tmdb: int, poster: str, by: str,
                by_id: Optional[Hashable] = None) -> Choice:
        """Add a film (the same film again returns the one already there)."""
        title = " ".join((title or "").split())[:MAX_TITLE]
        if not self.open or not title:
            raise ValueError("no open vote, or no title")
        for choice in self.choices:
            if choice.same_film(title, year, tmdb):
                return choice
        if len(self.choices) >= MAX_CHOICES or (
                by_id is not None and sum(1 for c in self.choices if c.by_id == by_id) >= PER_PERSON):
            raise VoteFull()
        choice = Choice(self._next_key, title, max(0, int(year)), max(0, int(tmdb)), poster or "",
                        (by or "")[:32], by_id=by_id)
        self._next_key += 1
        self.choices.append(choice)
        self._refresh()
        return choice

    def withdraw(self, key: int) -> Optional[Choice]:
        choice = self.find(key)
        if choice is None or not self.open:
            return None
        self.choices.remove(choice)
        for voter in [voter for voter, voted in self._ballots.items() if voted == key]:
            del self._ballots[voter]
        self._refresh()
        return choice

    def cast(self, voter: Hashable, name: str, key: int) -> bool:
        """The voter's vote goes to this film (0: taken back). Only while the vote is open."""
        if not self.open or (key and self.find(key) is None):
            return False
        self._names[voter] = (name or "")[:32]
        if key:
            self._ballots[voter] = key
        else:
            self._ballots.pop(voter, None)
        self._refresh()
        return True

    def ballot(self, voter: Hashable) -> int:
        return self._ballots.get(voter, 0)

    def mark_have(self, person: Hashable, name: str, keys: Iterable[int]) -> bool:
        """Which of the suggested films this person has. True if that changed anything."""
        mine = {key for key in keys if self.find(key) is not None}
        if self._have.get(person, set()) == mine and self._names.get(person) == name[:32]:
            return False
        self._names[person] = (name or "")[:32]
        self._have[person] = mine
        self._refresh()
        return True

    def keep_only(self, people: Iterable[Hashable]) -> bool:
        """Votes and films of people who left the room no longer count. True if someone was gone."""
        here = set(people)
        gone = [person for person in set(self._ballots) | set(self._have) if person not in here]
        for person in gone:
            self._ballots.pop(person, None)
            self._have.pop(person, None)
        if gone:
            self._refresh()
        return bool(gone)

    def close(self) -> Optional[Choice]:
        """Ends the vote: most votes wins (a tie: the film more people have, then the first suggested)."""
        if not self.open:
            return self.find(self.winner)
        self.open = False
        best = max(self.choices, key=lambda c: (len(c.voters), len(c.have), -c.key), default=None)
        self.winner = best.key if best is not None else 0
        self._changed()
        return best

    def cancel(self) -> None:
        self.open, self.choices, self.winner = False, [], 0
        self._ballots.clear()
        self._have.clear()
        self._changed()

    def _refresh(self) -> None:
        for choice in self.choices:
            choice.voters = sorted(self._names.get(v, "") for v, key in self._ballots.items() if key == choice.key)
            choice.have = sorted(self._names.get(p, "") for p, keys in self._have.items() if choice.key in keys)
        self._changed()

    def _changed(self) -> None:
        self.version += 1

    # ------------------------------------------------------------ the room message
    def to_message(self) -> Dict[str, Any]:
        return {"no": self.version, "open": self.open, "winner": self.winner,
                "choices": [choice.to_message() for choice in self.choices]}

    def load_message(self, message: Dict[str, Any]) -> bool:
        """A member takes over the host's vote (an already checked "vote" message). False if stale."""
        if message["no"] < self.version and message["no"] != 0:
            return False
        self.version = message["no"]
        self.open = bool(message["open"])
        self.choices = [Choice.from_message(raw) for raw in message["choices"]][:MAX_CHOICES]
        self.winner = int(message.get("winner") or 0)
        return True

    def reset(self) -> None:
        """Leaving the room: a member's copy of the host's vote goes (version counting starts over)."""
        self.open, self.choices, self.winner, self.version = False, [], 0, 0
        self._ballots.clear()
        self._have.clear()
