"""Watching in sync: what happens when anyone presses a button, for a whole room.

Rules (agreed for step 1, now for any number of people):
* Nothing is corrected automatically while the film plays. Jumping to fix a
  small gap is what caused the stutter in version 0.1. Instead the gap is
  measured and shown, and people fix it when *they* want:
    - "Sync me"   jumps only yourself to where the host is (no one else is disturbed);
    - "Sync up"   pauses everyone, lines everyone up on the host, then resumes together
                  after a short countdown.
* The host's app decides. Members' presses are *requests* that the host's app
  checks (cooldowns per person, "only the host controls") and then broadcasts.
  The host has no cooldowns. Because the check happens on the host's PC,
  editing your own copy of the app can't get around it.
* Play and pause can't be spammed (cooldown); skipping can (it's sent once,
  shortly after you stop pressing).
* "Watching on your own": your buttons only affect you and you ignore the
  room until you press Sync me / Rejoin. When the host has "only I control"
  on, members' buttons automatically work this way.
* Times are compared on the host's clock: members measure the difference to
  it with pings, so a message's travel time doesn't distort the gap.

Membership, keys and the relay are handled by syncwatch/room/session.py;
friends by syncwatch/room/friends.py. No Qt in here: the UI gives it a player,
calls ``tick()`` about ten times a second, passes it events with ``handle()``,
and asks it for the current state to draw.
"""
from __future__ import annotations

import base64
import collections
import logging
import math
import random
import statistics
import time
from dataclasses import dataclass, field
from typing import Callable, Deque, Dict, List, Optional, Set

from ..net.protocol import MAX_AVATAR_BYTES, b64
from ..room.codes import CodeError, parse
from ..room.context import Rooms
from ..room.friends import Friends
from ..room.session import JOINING, Member, RoomListener, RoomSession

log = logging.getLogger("syncwatch.sync")


def _whole_seconds(seconds: float) -> str:
    """Rounded up, so the last fraction of a second says "Try again in 1 s", not "in 0 s"."""
    return str(max(1, math.ceil(seconds)))


HEARTBEAT_HOST_S = 1.0
HEARTBEAT_MEMBER_S = 2.0
GAP_SAMPLES = 5
SEEK_RATE_PER_S = 5          # the host accepts at most this many seek requests per second per person
REJECT_GAP_S = 1.0           # "please wait" / "only the host" answers: at most one per second per person
PAUSE_ALIGN_MS = 150         # on pause, jump to the exact frame if further apart than this
PLAY_ALIGN_MS = 300          # on play, jump first if further apart than this
READY_TOLERANCE_MS = 500
MATCH_TOLERANCE_MS = 1000    # films whose lengths differ by less than this count as the same

REASONS_LEFT = {"left": "left", "timeout": "lost connection", "removed": "was removed by the host",
                "banned": "was removed and blocked by the host", "": "left"}


def fmt_time(ms: float) -> str:
    seconds = max(0, int(ms // 1000))
    return f"{seconds // 3600}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"


@dataclass
class Peer:
    """Someone else in the room, as far as watching goes."""
    member: Member
    avatar: Optional[bytes] = None
    film: Optional[Dict] = None
    pos: int = 0
    playing: bool = False
    separate: bool = False
    ready: bool = True
    at: int = 0                  # the room clock (ms) when ``pos`` was measured; 0 = unknown
    received_at: float = 0.0     # my clock (s) when the report arrived
    known: bool = False          # at least one report received
    match: str = ""              # film comparison with mine: same / similar / different

    @property
    def id(self) -> bytes:
        return self.member.id

    @property
    def name(self) -> str:
        return self.member.name

    @property
    def is_host(self) -> bool:
        return self.member.is_host


@dataclass
class Rules:
    host_only: bool = False
    cd_pp: float = 2.0
    cd_sync: float = 15.0


@dataclass
class _HostSync:
    id: int
    pos: int
    deadline: float
    by: str
    waiting: Set[bytes] = field(default_factory=set)   # who still has to be ready
    go_at: Optional[float] = None


class Controller(RoomListener):
    def __init__(self, player, ui, settings, event_sink: Callable[[Dict], None], rooms: Rooms,
                 clock: Callable[[], float] = time.monotonic):
        self.p = player
        self.ui = ui
        self.settings = settings
        self.clock = clock
        self.rooms = rooms
        self.session = RoomSession(rooms.identity, rooms.store, settings, self, event_sink, rooms.relay_for,
                                   rooms.default_relay, clock)
        self.friends = Friends(rooms.identity, rooms.store, settings, self, event_sink, rooms.relay_for,
                               rooms.default_relay, clock)
        self.name = "Me"
        self.avatar_png: Optional[bytes] = None
        self.film: Optional[Dict] = None
        self.peers: Dict[bytes, Peer] = {}
        self.attached = True
        self.rules = Rules()               # as decided by the host (or by me when I host)
        self.gaps: Deque[float] = collections.deque(maxlen=GAP_SAMPLES)
        self.status_text = "Not in a room"
        self._relay_problem_told = False
        self._next_heartbeat = 0.0
        self._next_gap_sample = 0.0
        self._pending_seek_at: Optional[float] = None
        self._my_cooldown_until: Dict[str, float] = {"pp": 0.0, "sync": 0.0}
        self._member_last: Dict[bytes, Dict[str, float]] = {}
        self._seek_times: Dict[bytes, Deque[float]] = {}
        self._last_reject: Dict[bytes, float] = {}
        self._host_sync: Optional[_HostSync] = None
        self._client_sync: Optional[Dict] = None
        self._play_at: Optional[float] = None
        self._sync_counter = 0
        self._align_on_next_state = False
        self._profile_due: Optional[float] = None
        self._warned_films: Set[bytes] = set()

    # ================================================================ setup
    def start(self) -> None:
        """Called once the window exists: start listening for friends' invitations."""
        self.friends.start()

    def shutdown(self) -> None:
        """SyncWatch is closing: say goodbye to the room and tell friends the room is gone."""
        if self.session.active:
            self.session.stop(quiet=True)
        if self.friends.hosting is not None:
            self.friends.hosting_stopped(wait=True)
        self.friends.stop()

    def set_profile(self, name: str, avatar_png: Optional[bytes]) -> None:
        old = self.name
        self.name = (name or "Me")[:32]
        self.avatar_png = avatar_png
        if self.session.in_room:
            if self.name != old:
                self.session.rename()
            self._send_profile()

    def set_film(self, name: str, size: int, duration_ms: int) -> None:
        film = {"name": name[:255], "size": int(size), "dur": max(0, int(duration_ms))}
        if film == self.film:
            return
        self.film = film
        self._warned_films.clear()
        for peer in self.peers.values():
            peer.match = ""
        self._next_heartbeat = 0.0
        self._compare_films()

    # ================================================================ state for the UI
    @property
    def connected(self) -> bool:
        """In a room (as host or member), so buttons may affect others."""
        return self.session.in_room

    @property
    def is_host(self) -> bool:
        return self.session.is_host

    @property
    def joining(self) -> bool:
        return self.session.role == JOINING

    @property
    def host_peer(self) -> Optional[Peer]:
        return next((p for p in self.peers.values() if p.is_host), None)

    def people(self) -> List[Peer]:
        """Everyone else in the room: the host first, then by when they joined."""
        return sorted(self.peers.values(), key=lambda p: (not p.is_host, p.member.joined))

    def my_rules(self) -> Rules:
        return Rules(bool(self.settings.get("sync.host_only")),
                     float(self.settings.get("sync.cooldown_playpause_s")),
                     float(self.settings.get("sync.cooldown_syncup_s")))

    def effective_rules(self) -> Rules:
        return self.my_rules() if self.is_host else self.rules

    def controls_room(self) -> bool:
        """Do my play/pause/skip buttons affect everyone right now?"""
        if not self.connected or not self.attached:
            return False
        return self.is_host or not self.effective_rules().host_only

    def can_sync_up(self) -> bool:
        return self.connected and (self.is_host or not self.effective_rules().host_only)

    def cooldown_left(self, action: str) -> float:
        if self.is_host or not self.connected:
            return 0.0
        return max(0.0, self._my_cooldown_until.get(action, 0.0) - self.clock())

    def estimate(self, peer: Peer, now: Optional[float] = None) -> Optional[float]:
        """Where someone's film is right now (ms), from their last report."""
        if peer is None or not peer.known:
            return None
        now = self.clock() if now is None else now
        if not peer.playing:
            return float(peer.pos)
        if peer.at and self.session.clock_ready:
            return peer.pos + (self.session.host_clock_ms(now) - peer.at)
        estimate = peer.pos + (now - peer.received_at) * 1000.0
        if self.settings.get("sync.compensate_latency"):
            estimate += self.session.rtt_ms() / 2.0
        return estimate

    def peer_estimate(self, now: Optional[float] = None) -> Optional[float]:
        """Where the host's film is (for members), used by Sync me and the seek-bar marker."""
        host = self.host_peer
        return self.estimate(host, now) if host is not None else None

    def reference_ms(self, now: Optional[float] = None) -> Optional[float]:
        """Where "the others" are, by the setting "Compare my position with": the host, or the
        median or average of everyone else watching along (so one person stuck loading doesn't skew it)."""
        mode = self.settings.get("sync.gap_reference")
        if mode == "host":
            return None if self.is_host else self.peer_estimate(now)
        estimates = [e for e in (self.estimate(p, now) for p in self.peers.values()
                                 if p.known and not p.separate) if e is not None]
        if not estimates:
            return None
        return statistics.mean(estimates) if mode == "average" else statistics.median(estimates)

    def reference_name(self) -> str:
        return "the host" if self.settings.get("sync.gap_reference") == "host" or len(self.peers) == 1 \
            else "the others"

    def gap_ms(self) -> Optional[float]:
        """How far my film is ahead (+) or behind (-) the others, smoothed. None if unknown."""
        if not self.gaps:
            return None
        return statistics.median(self.gaps)

    def peer_gap_ms(self, peer: Peer) -> Optional[float]:
        """How far someone is ahead (+) or behind (-) me (used by the host for everyone)."""
        if not peer.known or not self.p.has_media() or peer.separate:
            return None
        estimate = self.estimate(peer)
        return None if estimate is None else estimate - self.p.position()

    @property
    def syncing(self) -> bool:
        return self._host_sync is not None or self._client_sync is not None or self._play_at is not None

    # ================================================================ rooms
    def host_room(self) -> bool:
        return self.session.host_room()

    def join_room(self, text: str) -> bool:
        try:
            code = parse(text)
        except CodeError as exc:
            self.ui.problem("SW-407", str(exc), reason=str(exc))
            return False
        return self.session.join(code)

    def join_code(self, code) -> bool:
        return self.session.join(code)

    def leave(self) -> None:
        hosting = self.session.is_host
        self.session.stop()
        if hosting:
            self.friends.hosting_stopped()
        self._reset_room()
        self.ui.changed()

    def new_code(self) -> None:
        code = self.session.new_code()
        if code is not None:  # (friends hear about the new code when the room reopens)
            self.ui.activity("New room code made. Everyone in the room followed; the old code no longer works.",
                             "you")
        else:
            self.ui.activity("New room code made. The old code no longer works.", "you")
        self.ui.changed()

    def _reset_room(self) -> None:
        self.peers.clear()
        self.gaps.clear()
        self._host_sync = None
        self._client_sync = None
        self._play_at = None
        self.attached = True
        self._member_last.clear()
        self._seek_times.clear()
        self._last_reject.clear()
        self._warned_films.clear()
        self.rules = Rules()

    # ================================================================ user actions
    def toggle(self) -> None:
        (self.pause if self.p.is_playing() else self.play)()

    def play(self) -> None:
        self._play_pause("play")

    def pause(self) -> None:
        self._play_pause("pause")

    def _play_pause(self, action: str) -> None:
        self._cancel_countdown_if_local()
        if not self.connected:
            self._local(action)
            return
        if not self.controls_room():
            self._go_separate(action)
            self._local(action)
            return
        if self.is_host:
            self._local(action)
            self._broadcast_cmd(action, by=None)
            self.ui.activity(f"You {'paused' if action == 'pause' else 'pressed play'} at {fmt_time(self.p.position())}", "you")
            return
        left = self.cooldown_left("pp")
        if left > 0:
            self.ui.problem("SW-502", seconds=_whole_seconds(left))
            return
        self._my_cooldown_until["pp"] = self.clock() + self.rules.cd_pp
        self.session.send_room("req", action=action, pos=int(self.p.position()))
        self.ui.changed()

    def skip(self, delta_ms: int) -> None:
        self.seek(self.p.position() + delta_ms)

    def seek(self, ms: float) -> None:
        """Jump locally at once; tell the others once the skipping stops (debounced)."""
        self._cancel_countdown_if_local()
        target = self._clamp(ms)
        self.p.seek(target)
        if not self.connected:
            return
        if not self.controls_room():
            self._go_separate("seek")
            return
        debounce = int(self.settings.get("sync.seek_debounce_ms")) / 1000.0
        self._pending_seek_at = self.clock() + debounce

    def sync_up(self) -> None:
        if not self.connected or not self.peers:
            self.ui.activity("Nobody to sync with yet", "info")
            return
        if self.is_host:
            self._start_host_sync(by="")
            return
        if self.effective_rules().host_only:
            self.ui.problem("SW-504")
            return
        left = self.cooldown_left("sync")
        if left > 0:
            self.ui.problem("SW-502", seconds=_whole_seconds(left))
            return
        self._my_cooldown_until["sync"] = self.clock() + self.rules.cd_sync
        self.session.send_room("req", action="syncup")
        self.ui.activity("You asked everyone to sync up", "you")
        self.ui.changed()

    def sync_me(self) -> None:
        """Jump only myself to where the host is (and follow the room again)."""
        host = self.host_peer
        target = self.peer_estimate()
        if target is None or host is None:
            self.ui.activity("Nothing to sync to yet", "info")
            return
        before = self.p.position()
        target = self._clamp(target)
        self.p.seek(target)
        if host.playing and not self.p.is_playing():
            self.p.play()
        elif not host.playing and self.p.is_playing():
            self.p.pause()
        was_separate = not self.attached
        self.attached = True
        self.gaps.clear()
        self._next_heartbeat = 0.0
        jump = (target - before) / 1000.0
        verb = "Rejoined the room" if was_separate else "Synced to " + host.name
        self.ui.activity(f"{verb} ({jump:+.1f} s)", "you")
        self.ui.changed()

    def set_attached(self, attached: bool) -> None:
        if self.is_host:
            return  # the host is the reference everyone syncs to, so it can't "leave" the room
        if attached and not self.attached:
            self.sync_me()
        elif not attached and self.attached:
            self.attached = False
            self._next_heartbeat = 0.0
            self.ui.activity("You're watching on your own. Press Sync me to rejoin.", "you")
            self.ui.changed()

    # host actions on people
    def approve(self, key_id: bytes) -> None:
        self.session.approve(key_id)
        self.ui.changed()

    def approve_all(self) -> None:
        self.session.approve_all()
        self.ui.changed()

    def deny(self, key_id: bytes, block: bool = False) -> None:
        knock = self.session.waiting.get(key_id)
        if self.session.deny(key_id, block) and knock is not None:
            self.ui.activity(f"You {'blocked' if block else 'turned away'} {knock.name}", "you")
        self.ui.changed()

    def kick(self, key_id: bytes, ban: bool = False) -> None:
        self.session.kick(key_id, ban)
        self.ui.changed()

    # ================================================================ called by the UI timer
    def tick(self) -> None:
        now = self.clock()
        self.session.tick()
        self.friends.tick(self.session)
        if self._pending_seek_at is not None and now >= self._pending_seek_at:
            self._pending_seek_at = None
            self._send_seek()
        self._advance_host_sync(now)
        self._advance_client_sync(now)
        if self._play_at is not None and now >= self._play_at:
            self._play_at = None
            self.p.play()
            self.ui.changed()
        if self._profile_due is not None and now >= self._profile_due:
            self._profile_due = None
            self._send_profile()
        if self.connected and now >= self._next_heartbeat:
            self._next_heartbeat = now + (HEARTBEAT_HOST_S if self.is_host else HEARTBEAT_MEMBER_S)
            self._send_heartbeat()
        if self.connected and now >= self._next_gap_sample:
            self._next_gap_sample = now + 1.0
            self._sample_gap(now)

    def _sample_gap(self, now: float) -> None:
        if not (self.p.has_media() and self.attached and not self.syncing and self._pending_seek_at is None):
            return
        reference = self.reference_ms(now)
        if reference is not None:
            self.gaps.append(self.p.position() - reference)

    # ================================================================ events from the network
    def handle(self, event: Dict) -> None:
        kind = event.get("t")
        try:
            if kind in ("_relay", "_relay_state"):
                self.session.handle_event(event)
            elif kind in ("_mail", "_mail_state"):
                self.friends.handle_event(event)
        finally:
            self.ui.changed()

    # RoomListener ------------------------------------------------------
    def room_changed(self, what: str, **info) -> None:
        handler = getattr(self, f"_room_{what}", None)
        if handler is not None:
            handler(**info)
        self.ui.changed()

    def room_problem(self, code: str, technical: str = "", **params) -> None:
        self.ui.problem(code, technical, **params)

    def friend_message(self, sender, member, body: Dict) -> None:
        self.friends.handle_room(self.session, sender, member, body)

    def friends_changed(self, what: str, **info) -> None:
        """From Friends: requests, new friends, friends' rooms."""
        name = info.get("name", "")
        texts = {"request": f"{name} wants to add you as a friend",
                 "added": f"{name} is now your friend",
                 "declined": f"{name} didn't accept your friend request",
                 "removed": f"{name} is no longer on your friend list",
                 "hosting": f"{name} is hosting. Join from the Friends list",
                 "requested": f"Friend request sent to {name}"}
        if what in texts and name:
            self.ui.activity(texts[what], "friend" if what != "requested" else "you")
        self.ui.changed()

    def _set_status(self, text: str) -> None:
        self.status_text = text
        self.ui.status(text, self.connected)

    def _room_hosting(self, code, room: str) -> None:
        self._reset_room()
        self._set_status("Hosting your room")
        self.ui.activity("Your room is open. Send friends the room code (Copy code).", "you")
        self.friends.hosting_started(code, room)
        self._next_heartbeat = 0.0

    def _room_connection(self, state: str, error) -> None:
        if state == "connected":
            self._relay_problem_told = False
            if self.session.is_host:
                self._set_status("Hosting your room")
            elif self.session.in_room:
                self._set_status(f"In {self.session.room_name}")
        elif state == "waiting" and error is not None:
            self._set_status("Can't reach the relay server. Trying again…")
            if not self._relay_problem_told:  # once per outage, not at every retry
                self._relay_problem_told = True
                self.ui.problem(error.code, error.technical)
        elif state == "connecting" and not self.session.in_room:
            self._set_status("Connecting…")

    def _room_joining(self, moving: bool = False) -> None:
        self._reset_room()
        self._set_status("Connecting…")

    def _room_nobody(self) -> None:
        self._set_status("Nobody is hosting this room right now. Waiting…")
        self.ui.problem("SW-415")

    def _room_asking(self, host: str) -> None:
        self._set_status(f"Asking {host} to let you in…")
        self.ui.activity(f"Asking {host} to let you in…", "info")

    def _room_joined(self, room: str, host: str, moving: bool = False, rejoined: bool = False) -> None:
        self._sync_peers()
        self._set_status(f"In {room}")
        if rejoined:
            self.ui.activity(f"Back in {room}", "friend")
        elif moving:
            self.ui.activity(f"{host} made a new room code; you followed automatically", "friend")
        else:
            self.ui.activity(f"You're in {room}", "friend")
        self._align_on_next_state = not rejoined
        self._next_heartbeat = 0.0
        self._profile_due = self.clock()

    def _room_rejoining(self) -> None:
        self._set_status("Reconnecting to the room…")
        self.ui.activity("Reconnecting to the room…", "warn")

    def _room_denied(self, reason: str) -> None:
        self._reset_room()
        self._set_status("Not in a room")
        code = {"full": "SW-410", "friends_only": "SW-416", "version": "SW-413", "busy": "SW-417"}.get(reason, "SW-409")
        self.ui.problem(code, f"host answered: {reason}")

    def _room_kicked(self, ban: bool) -> None:
        self._reset_room()
        self._set_status("Not in a room")
        self.ui.problem("SW-411", "blocked" if ban else "removed")

    def _room_closed(self, host: str) -> None:
        self._reset_room()
        self._set_status("Not in a room")
        self.ui.problem("SW-412", host=host)

    def _room_closed_by_me(self) -> None:
        self._reset_room()
        self._set_status("Not in a room")
        self.ui.activity("You closed your room", "you")

    def _room_left(self) -> None:
        self._reset_room()
        self._set_status("Not in a room")
        self.ui.activity("You left the room", "you")

    def _room_host_lost(self) -> None:
        self._set_status("Lost contact with the host. Waiting…")
        self.ui.activity("Lost contact with the host. Your film keeps playing.", "warn")

    def _room_host_back(self) -> None:
        self._set_status(f"In {self.session.room_name}")
        self.ui.activity("The host is back", "friend")

    def _room_moved(self) -> None:
        self.ui.activity("The host made a new room code. Following…", "info")

    def _room_member_joined(self, member: Member) -> None:
        self._sync_peers()
        self.ui.activity(f"{member.name} joined", "friend")
        # So the newcomer gets everyone's name and picture, and the host's state at once.
        self._profile_due = self.clock() + random.uniform(0.1, 1.0)
        if self.is_host:
            self._next_heartbeat = 0.0

    def _room_member_left(self, member: Member, reason: str) -> None:
        self._sync_peers()
        self.ui.activity(f"{member.name} {REASONS_LEFT.get(reason, 'left')}", "warn" if reason == "timeout" else "friend")
        sync = self._host_sync
        if sync is not None:
            sync.waiting.discard(member.id)

    def _room_members(self) -> None:
        self._sync_peers()

    def _room_waiting(self, knock=None) -> None:
        if knock is not None:
            tags = "friend" if knock.friend else "new person"
            self.ui.activity(f"{knock.name} ({knock.key.short_id}, {tags}) wants to join", "warn")
            request = getattr(self.ui, "join_request", None)
            if request is not None:
                request(knock)

    def _sync_peers(self) -> None:
        members = {m.id: m for m in self.session.others()}
        for key_id in list(self.peers):
            if key_id not in members:
                del self.peers[key_id]
                self._member_last.pop(key_id, None)
                self._seek_times.pop(key_id, None)
                self._last_reject.pop(key_id, None)
        for key_id, member in members.items():
            peer = self.peers.get(key_id)
            if peer is None:
                self.peers[key_id] = Peer(member)
            else:
                peer.member = member

    def room_message(self, member: Member, body: Dict) -> None:
        peer = self.peers.get(member.id)
        if peer is None:
            self._sync_peers()
            peer = self.peers.get(member.id)
            if peer is None:
                return
        handler = getattr(self, f"_on_{body['t']}", None)
        if handler is None:
            log.debug("ignoring message %s", body["t"])
            return
        handler(peer, body)

    # message handlers ----------------------------------------------------
    def _on_profile(self, peer: Peer, body: Dict) -> None:
        name = body["name"].strip() or peer.name
        self.session.rename_member(peer.member, name)
        peer.member.name = name
        peer.avatar = self._decode_avatar(body.get("avatar"))  # no picture in it = they removed theirs

    def _report(self, peer: Peer, body: Dict) -> None:
        peer.pos = body["pos"]
        peer.playing = body["playing"]
        peer.at = body["at"]
        peer.received_at = self.clock()
        peer.ready = body.get("ready", True)
        peer.known = True
        film = body.get("film")
        if film != peer.film:
            peer.film = film
            peer.match = ""
            self._compare_films()

    def _on_status(self, peer: Peer, body: Dict) -> None:
        """A member's heartbeat (everyone sees them: for the people list and the gap)."""
        self._report(peer, body)
        peer.separate = body.get("separate", False)

    def _on_state(self, peer: Peer, body: Dict) -> None:
        """The host's heartbeat."""
        self._report(peer, body)
        peer.separate = False
        rules = body["rules"]
        old = self.rules.host_only
        self.rules = Rules(rules["host_only"], rules.get("cd_pp", 2.0), rules.get("cd_sync", 15.0))
        if self.rules.host_only != old:
            self.ui.activity("The host now controls playback alone" if self.rules.host_only
                             else "Everyone can control playback again", "friend")
        if self._align_on_next_state and self.p.has_media():
            self._align_on_next_state = False
            self.sync_me()

    def _on_req(self, peer: Peer, body: Dict) -> None:
        if not self.is_host:
            return
        action = body["action"]
        rules = self.my_rules()
        now = self.clock()
        last = self._member_last.setdefault(peer.id, {"pp": -1e9, "sync": -1e9})
        if rules.host_only:
            self._reject(peer, action, "host_only")
            return
        if action in ("play", "pause"):
            wait = last["pp"] + rules.cd_pp - now
            if wait > 0:
                self._reject(peer, action, "cooldown", wait)
                return
            last["pp"] = now
            # Play always continues from the host's position, so everyone resumes together.
            self._local(action)
            self._broadcast_cmd(action, by=peer)
            self.ui.activity(f"{peer.name} {'paused' if action == 'pause' else 'pressed play'} at {fmt_time(self.p.position())}", "friend")
        elif action == "seek":
            times = self._seek_times.setdefault(peer.id, collections.deque(maxlen=SEEK_RATE_PER_S))
            if len(times) == SEEK_RATE_PER_S and now - times[0] < 1.0:
                return  # flood protection: silently drop
            times.append(now)
            if "pos" not in body:
                return
            self.p.seek(self._clamp(body["pos"]))
            self._broadcast_cmd("seek", by=peer, position=body["pos"])
            self.ui.activity(f"{peer.name} jumped to {fmt_time(body['pos'])}", "friend")
        elif action == "syncup":
            if self._host_sync is not None:
                self._reject(peer, action, "busy")
                return
            wait = last["sync"] + rules.cd_sync - now
            if wait > 0:
                self._reject(peer, action, "cooldown", wait)
                return
            last["sync"] = now
            self._start_host_sync(by=peer.name)

    def _reject(self, peer: Peer, action: str, reason: str, wait: float = 0.0) -> None:
        now = self.clock()
        if now - self._last_reject.get(peer.id, -1e9) < REJECT_GAP_S:
            return  # someone pressing (or sending) far too often doesn't get an answer each time
        self._last_reject[peer.id] = now
        fields = {"to": b64(peer.id), "action": action, "reason": reason}
        if wait:
            fields["wait"] = round(wait, 2)
        self.session.send_room("reject", **fields)

    def _on_cmd(self, peer: Peer, body: Dict) -> None:
        if self.is_host or not peer.is_host:
            return
        action, pos = body["action"], body["pos"]
        # A command also tells us exactly where the host is now, even if we ignore it below;
        # without this, "Sync me" could use host information up to a second old.
        peer.pos, peer.playing, peer.at = pos, body["playing"], body["at"]
        peer.received_at = self.clock()
        peer.known = True
        mine = body.get("by_id") == self.rooms.identity.public.key_id
        who = "You" if mine else (body.get("by") or peer.name)
        if not self.attached:
            self.ui.activity(f"{who} {_verb(action)} {'to' if action == 'seek' else 'at'} {fmt_time(pos)} "
                             "(you're watching on your own)", "friend")
            return
        target = self.estimate(peer)
        target = float(pos) if target is None else target
        if self._pending_seek_at is not None:
            # I'm jumping somewhere myself right now: keep my spot, my jump follows in a moment.
            self._local("play" if body["playing"] else "pause")
            return
        if action == "pause":
            self.p.pause()
            if abs(self.p.position() - pos) > PAUSE_ALIGN_MS:
                self.p.seek(pos)
        elif action == "play":
            if abs(self.p.position() - target) > PLAY_ALIGN_MS:
                self.p.seek(self._clamp(target))
            self.p.play()
        else:
            target = self._clamp(target)
            if abs(self.p.position() - target) > PLAY_ALIGN_MS:
                self.p.seek(target)  # (my own skip comes back here: no second jump)
            if body["playing"] and not self.p.is_playing():
                self.p.play()
            elif not body["playing"] and self.p.is_playing():
                self.p.pause()
        self.gaps.clear()
        self.ui.activity(f"{who} {_verb(action)} {'to' if action == 'seek' else 'at'} {fmt_time(pos)}",
                         "you" if mine else "friend")

    def _on_reject(self, peer: Peer, body: Dict) -> None:
        if not peer.is_host or body["to"] != self.rooms.identity.public.key_id:
            return
        reason = body["reason"]
        if reason == "cooldown":
            wait = body.get("wait", 1.0)
            self._my_cooldown_until["pp" if body["action"] in ("play", "pause") else "sync"] = self.clock() + wait
            self.ui.problem("SW-502", seconds=_whole_seconds(wait))
        elif reason == "host_only":
            self.ui.problem("SW-504")
        else:
            self.ui.activity("A sync is already in progress", "info")

    def _on_sync_prepare(self, peer: Peer, body: Dict) -> None:
        if self.is_host or not peer.is_host:
            return
        who = body.get("by") or peer.name
        if not self.attached:
            self.ui.activity(f"{who} synced everyone up (you're watching on your own)", "friend")
            return
        self._play_at = None
        self.p.pause()
        self.p.seek(body["pos"])
        self._client_sync = {"id": body["id"], "pos": body["pos"], "sent": False}
        self.ui.activity(f"{who} is syncing everyone up…", "friend")

    def _on_sync_ready(self, peer: Peer, body: Dict) -> None:
        sync = self._host_sync
        if self.is_host and sync is not None and body["id"] == sync.id:
            sync.waiting.discard(peer.id)

    def _on_sync_go(self, peer: Peer, body: Dict) -> None:
        client = self._client_sync
        if self.is_host or not peer.is_host or client is None or body["id"] != client["id"]:
            return
        self._client_sync = None
        start = self.session.local_from_host(body["start"]) if self.session.clock_ready else None
        now = self.clock()
        if start is None:
            start = now + float(self.settings.get("sync.countdown_s"))
        self._play_at = max(now, start)
        self.ui.countdown(int(max(0.0, self._play_at - now) * 1000))
        self.gaps.clear()

    def _on_sync_abort(self, peer: Peer, body: Dict) -> None:
        if self._client_sync is not None and body["id"] == self._client_sync["id"]:
            self._client_sync = None
            self._play_at = None

    # ================================================================ helpers
    def _local(self, action: str) -> None:
        if action == "play":
            self.p.play()
        else:
            self.p.pause()

    def _go_separate(self, action: str) -> None:
        if self.attached:
            self.attached = False
            self._next_heartbeat = 0.0
            reason = ("The host controls playback, so your" if self.effective_rules().host_only
                      else "Your")
            self.ui.activity(f"{reason} buttons now work just for you. Press Sync me to rejoin.", "you")
            self.gaps.clear()
            self.ui.changed()

    def _cancel_countdown_if_local(self) -> None:
        if self._play_at is not None and (self.is_host or not self.connected):
            self._play_at = None
            self._host_sync = None

    def _broadcast_cmd(self, action: str, by: Optional[Peer], position: Optional[float] = None) -> None:
        pos = int(self.p.position() if position is None else position)
        fields = {"action": action, "pos": max(0, pos), "playing": self.p.is_playing(),
                  "at": self.session.host_clock_ms()}
        if by is not None:
            fields["by"] = by.name
            fields["by_id"] = b64(by.id)
        self.session.send_room("cmd", **fields)
        self.gaps.clear()

    def _send_seek(self) -> None:
        if not self.controls_room():
            return
        if self.is_host:
            self._broadcast_cmd("seek", by=None)
            self.ui.activity(f"You jumped to {fmt_time(self.p.position())}", "you")
        else:
            # Where my film is now, not where I jumped to: if it's playing, it has moved on since
            # (the debounce), and the others should land where I am.
            self.session.send_room("req", action="seek", pos=max(0, int(self.p.position())))

    def _send_heartbeat(self) -> None:
        fields = {"pos": max(0, int(self.p.position())), "playing": self.p.is_playing(),
                  "ready": bool(self.p.is_ready()),
                  "at": self.session.host_clock_ms() if self.session.clock_ready else 0}
        if self.film:
            fields["film"] = self.film
        if self.is_host:
            rules = self.my_rules()
            fields["rules"] = {"host_only": rules.host_only, "cd_pp": rules.cd_pp, "cd_sync": rules.cd_sync}
            self.session.send_room("state", **fields)
        else:
            fields["separate"] = not self.attached
            self.session.send_room("status", **fields)

    def _send_profile(self) -> None:
        fields = {"name": self.name}
        avatar = self._network_avatar()
        if avatar:
            fields["avatar"] = base64.b64encode(avatar).decode("ascii")
        self.session.send_room("profile", **fields)

    def _network_avatar(self) -> Optional[bytes]:
        data = self.avatar_png
        if data and len(data) > MAX_AVATAR_BYTES:
            shrink = getattr(self.ui, "shrink_avatar", None)
            data = shrink(data, MAX_AVATAR_BYTES) if shrink is not None else None
            if data and len(data) > MAX_AVATAR_BYTES:
                data = None
        return data

    def _start_host_sync(self, by: str) -> None:
        if self._host_sync is not None:
            return
        self._sync_counter += 1
        pos = int(self.p.position())
        self.p.pause()
        self.p.seek(pos)
        timeout = float(self.settings.get("sync.ready_timeout_s"))
        waiting = {p.id for p in self.peers.values() if not p.separate}  # those on their own aren't waited for
        self._host_sync = _HostSync(self._sync_counter, pos, self.clock() + timeout, by, waiting)
        fields = {"id": self._sync_counter, "pos": pos}
        if by:
            fields["by"] = by
        self.session.send_room("sync_prepare", **fields)
        self.ui.activity(f"{by or 'You'} started Sync up at {fmt_time(pos)}", "friend" if by else "you")

    def _advance_host_sync(self, now: float) -> None:
        sync = self._host_sync
        if sync is None or sync.go_at is not None:
            if sync is not None and sync.go_at is not None and self._play_at is None:
                self._host_sync = None  # finished
            return
        if not self.connected:
            self._host_sync = None
            return
        sync.waiting &= set(self.peers)       # people who left aren't waited for
        me_ready = self.p.is_ready() and abs(self.p.position() - sync.pos) < READY_TOLERANCE_MS
        timed_out = now >= sync.deadline
        if not ((not sync.waiting and me_ready) or timed_out):
            return
        if timed_out and sync.waiting:
            names = ", ".join(self.peers[k].name for k in sync.waiting if k in self.peers)
            self.ui.problem("SW-503", name=names or "Someone")
        delay_ms = int(self.settings.get("sync.countdown_s")) * 1000
        start = self.session.host_clock_ms(now) + delay_ms
        self.session.send_room("sync_go", id=sync.id, start=start)
        sync.go_at = now + delay_ms / 1000.0
        self._play_at = sync.go_at
        self.ui.countdown(delay_ms)
        self.gaps.clear()

    def _advance_client_sync(self, now: float) -> None:
        client = self._client_sync
        if client is None or client["sent"]:
            return
        if self.p.is_ready() and abs(self.p.position() - client["pos"]) < READY_TOLERANCE_MS:
            client["sent"] = True
            self.session.send_room("sync_ready", id=client["id"])

    def _clamp(self, ms: float) -> float:
        ms = max(0.0, float(ms))
        duration = self.p.duration()
        return min(ms, max(0.0, duration - 500.0)) if duration > 0 else ms

    def _decode_avatar(self, data: Optional[str]) -> Optional[bytes]:
        if not data:
            return None
        try:
            raw = base64.b64decode(data, validate=True)
        except (ValueError, TypeError):
            self.ui.problem("SW-803", "avatar is not valid base64")
            return None
        if not (raw.startswith(b"\x89PNG\r\n\x1a\n") or raw.startswith(b"\xff\xd8\xff")):
            self.ui.problem("SW-803", "avatar is not a PNG or JPEG")
            return None
        return raw

    def _compare_films(self) -> None:
        if not self.film:
            return
        mine = self.film
        for peer in self.peers.values():
            theirs = peer.film
            if not theirs:
                continue
            if mine["size"] == theirs["size"] and mine["name"] == theirs["name"]:
                state = "same"
            elif mine.get("dur") and theirs.get("dur") and abs(mine["dur"] - theirs["dur"]) <= MATCH_TOLERANCE_MS:
                state = "similar"
            elif not mine.get("dur") or not theirs.get("dur"):
                continue  # wait until both lengths are known
            else:
                state = "different"
            if state == peer.match:
                continue
            peer.match = state
            if state == "different" and peer.id not in self._warned_films:
                self._warned_films.add(peer.id)
                text = (f"{peer.name}'s film is {fmt_time(theirs['dur'])} long ({theirs['name']}), yours is "
                        f"{fmt_time(mine['dur'])} ({mine['name']}).")
                self.ui.problem("SW-501", detail=text)
            elif state == "similar":
                self.ui.activity(f"{peer.name} has a different file of the same length. It should line up.", "info")


def _verb(action: str) -> str:
    return {"pause": "paused", "play": "pressed play", "seek": "jumped"}.get(action, action)
