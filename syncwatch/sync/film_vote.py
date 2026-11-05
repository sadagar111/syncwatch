"""The film vote's room part: who may do what, the requests, sending the vote out.

The vote itself is discover/vote.py (no network, no Qt). This is what the controller hands the
"vote" and "vreq" messages to and calls every tick; the window calls the actions (start, suggest,
cast, close, cancel) and is told about changes (ui.vote_changed, ui.vote_won).

Rules: anyone in the room may start a vote and suggest films (at most 3 each, 12 in all) and
take their own suggestion back; one vote per person, changeable while the vote is open; only the
host closes or cancels it (and may take any suggestion out). Requests are flood-limited like the
queue's. Alone (not in a room) there's no vote: the Discover window's "Pick one for me" is for that.
"""
from __future__ import annotations

import collections
import logging
from typing import Callable, Deque, Dict, List, Optional, Tuple

from ..discover.vote import Choice, Vote, VoteFull

log = logging.getLogger("syncwatch.vote")

VREQ_RATE = 8               # the host takes at most this many vote requests per person ...
VREQ_WINDOW_S = 10.0        # ... in this many seconds (more are dropped)
VOTE_RESEND_S = 20.0        # the host repeats the vote now and then (for people who just came in)
HAVE_RATE = 6               # "I have these films" messages the host takes per person in VREQ_WINDOW_S
HAVE_GAP_S = 5.0            # a member says it again (if the host's vote still shows it wrong) after this


class FilmVoteRoom:
    def __init__(self, controller) -> None:
        self.c = controller
        self.vote = Vote()
        self.my_vote = 0                     # the film I voted for (as far as I know)
        # which of these films do I have? (the window sets this: it knows the library)
        self.have_check: Callable[[List[Choice]], List[int]] = lambda choices: []
        self._from_host = False              # my vote is a copy of the host's
        self._dirty = False
        self._sent_at = -1e9
        self._times: Dict[bytes, Deque[float]] = {}
        self._have_times: Dict[bytes, Deque[float]] = {}
        self._told_have: Optional[Tuple[int, Tuple[int, ...]]] = None   # (round, keys) last sent to the host
        self._told_at = -1e9
        self._round = 0                      # counts votes started (a new vote: tell "have" again)

    # ------------------------------------------------------------ for the window
    @property
    def decides(self) -> bool:
        """My app holds the vote: I host the room."""
        return self.c.connected and self.c.is_host

    @property
    def available(self) -> bool:
        return self.c.connected

    def start(self) -> None:
        if self.decides:
            self._do("start", None)
        elif self.available:
            self.c.session.send_room("vreq", op="start")

    def suggest(self, title: str, year: int = 0, tmdb: int = 0, poster: str = "") -> None:
        fields = {"title": " ".join(title.split())[:120], "year": max(0, min(2200, int(year))),
                  "tmdb": max(0, int(tmdb))}
        if poster:
            fields["poster"] = poster
        if self.decides:
            self._do("suggest", None, **fields)
        elif self.available:
            self.c.session.send_room("vreq", op="suggest", **fields)    # (starts a vote if none is on)
            self.c.ui.activity(f"You suggested {fields['title']}", "you")

    def withdraw(self, key: int) -> None:
        if self.decides:
            self._do("withdraw", None, k=key)
        elif self.available:
            self.c.session.send_room("vreq", op="withdraw", k=key)

    def cast(self, key: int) -> None:
        """My vote for this film (0 or the film I already voted for: taken back)."""
        key = 0 if key == self.my_vote else key
        if self.decides:
            old, self.my_vote = self.my_vote, key
            if self._do("cast", None, k=key) is not None:
                self.my_vote = old
        elif self.available and self.vote.open:
            self.my_vote = key
            self.c.session.send_room("vreq", op="cast", k=key)
            self._changed_here()

    def close(self) -> None:
        if self.decides and self.vote.open:
            self._announce(self.vote.close(), None)
            self._changed()

    def cancel(self) -> None:
        if self.decides and self.vote.active:
            self.vote.cancel()
            self.my_vote = 0
            self.c.ui.activity("You cancelled the film vote", "you")
            self._changed()

    def library_changed(self) -> None:
        """The library changed: maybe I have one of the films now."""
        self._tell_have()

    # ------------------------------------------------------------ the host's side
    def _me(self) -> bytes:
        return self.c.rooms.identity.public.key_id

    def _do(self, op: str, peer, **fields) -> Optional[str]:
        """Change the vote (host). peer: who asked (None = me). Returns why not, or None."""
        who = peer.name if peer is not None else self.c.name
        voter = peer.id if peer is not None else self._me()
        mine = peer is None
        if op == "start":
            if self.vote.open:
                return None
            self.vote.start()
            self._round += 1
            self.my_vote = 0
            self._told_have = None
            self.c.ui.activity(f"{'You' if mine else who} started a film vote: suggest films and vote",
                               "you" if mine else "friend")
        elif op == "suggest":
            if not self.vote.open:
                self._do("start", peer)
            try:
                before = len(self.vote.choices)
                choice = self.vote.suggest(fields.get("title", ""), int(fields.get("year") or 0),
                                           int(fields.get("tmdb") or 0), fields.get("poster", ""), who, by_id=voter)
            except VoteFull:
                if mine:
                    self.c.ui.activity("The vote has enough films (12 in all, 3 each)", "warn")
                return "busy"
            except ValueError:
                return "bad"
            if len(self.vote.choices) > before:
                self.c.ui.activity(f"{'You' if mine else who} suggested {choice.label}", "you" if mine else "friend")
                self._tell_have()
        elif op == "withdraw":
            choice = self.vote.find(int(fields.get("k") or 0))
            if choice is None:
                return None
            if not mine and choice.by_id != voter:
                return "host_only"
            self.vote.withdraw(choice.key)
            if self.my_vote == choice.key:
                self.my_vote = 0
        elif op == "cast":
            if not self.vote.cast(voter, who, int(fields.get("k") or 0)):
                return "closed"
        elif op == "have":
            self.vote.mark_have(voter, who, fields.get("keys") or [])
        else:
            return "bad"
        self._changed()
        return None

    def on_vreq(self, peer, body: Dict) -> None:
        if not self.decides:
            return
        now = self.c.clock()
        have = body["op"] == "have"     # (sent by the app itself, counted on its own)
        rate = HAVE_RATE if have else VREQ_RATE
        times = (self._have_times if have else self._times).setdefault(peer.id, collections.deque(maxlen=rate))
        if len(times) == rate and now - times[0] < VREQ_WINDOW_S:
            return   # flood protection: silently dropped
        times.append(now)
        fields = {name: body[name] for name in ("k", "title", "year", "tmdb", "poster", "keys") if name in body}
        why = self._do(body["op"], peer, **fields)
        if why in ("busy", "host_only"):
            self.c._reject(peer, "vote", why)

    def tick(self, now: float) -> None:
        if not self.decides:
            return
        if self.vote.active and self.vote.keep_only([p.id for p in self.c.peers.values()] + [self._me()]):
            self._dirty = True
        if self._dirty or (self.vote.active and now - self._sent_at > VOTE_RESEND_S):
            self._dirty = False
            self._sent_at = now
            self.c.session.send_room("vote", **self.vote.to_message())

    # ------------------------------------------------------------ a member's side
    def on_vote(self, peer, body: Dict) -> None:
        if self.c.is_host or not peer.is_host:
            return
        was_open, old_winner = self.vote.open, self.vote.winner
        before = {choice.key for choice in self.vote.choices}
        if not self.vote.load_message(body):
            return
        self._from_host = True
        if self.vote.open and not was_open:
            self._round += 1
            self.my_vote = 0
            self._told_have = None
            self.c.ui.activity("A film vote is on: suggest films and vote", "friend")
        for choice in self.vote.choices:
            if before and choice.key not in before and choice.by != self.c.name:
                self.c.ui.activity(f"{choice.by or 'Someone'} suggested {choice.label}", "friend")
        if self.my_vote and self.vote.find(self.my_vote) is None:
            self.my_vote = 0
        if not self.vote.open and self.vote.winner and self.vote.winner != old_winner:
            self._announce(self.vote.find(self.vote.winner), peer)
        self._tell_have()
        self._changed_here()

    def reset(self) -> None:
        """Leaving a room: a member's copy of the host's vote goes; a host's own vote ends."""
        self._times.clear()
        self._have_times.clear()
        self._from_host = False
        self.vote.reset()
        self.my_vote = 0
        self._told_have = None
        self._changed_here()

    # ------------------------------------------------------------ shared
    def _tell_have(self) -> None:
        """Which suggested films I have: the host's app notes its own; a member tells the host when the
        host's vote shows it differently (again after HAVE_GAP_S if it still does: the host repeats
        the vote every VOTE_RESEND_S, so a lost message is made good)."""
        if not self.vote.open or not self.c.connected:
            return
        try:
            keys = tuple(sorted(set(self.have_check(list(self.vote.choices)))))
        except Exception:  # noqa: BLE001 - the library check must never break the room
            log.exception("checking the library for the vote's films failed")
            return
        if self.decides:
            if self.vote.mark_have(self._me(), self.c.name, keys):
                self._changed()
            return
        shown = tuple(sorted(c.key for c in self.vote.choices if self.c.name in c.have))
        now = self.c.clock()
        if keys == shown or (self._told_have == (self._round, keys) and now - self._told_at < HAVE_GAP_S):
            return
        self._told_have, self._told_at = (self._round, keys), now
        self.c.session.send_room("vreq", op="have", keys=list(keys))

    def _announce(self, winner: Optional[Choice], peer) -> None:
        if winner is None:
            self.c.ui.activity("The film vote ended without films", "info")
            return
        votes = len(winner.voters)
        self.c.ui.activity(f"The vote is closed: {winner.label} won ({votes} vote{'s' if votes != 1 else ''})",
                           "you" if peer is None else "friend")
        won = getattr(self.c.ui, "vote_won", None)
        if won is not None:
            won(winner)

    def _changed(self) -> None:
        self._dirty = True
        self._changed_here()

    def _changed_here(self) -> None:
        changed = getattr(self.c.ui, "vote_changed", None)
        if changed is not None:
            changed()
