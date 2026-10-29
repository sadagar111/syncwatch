"""Being in a room: hosting it or joining it, through the relay, safely.

This is the membership part (docs/ROOMS.md, "Joining a room"): who is in the
room, who may get in, and the keys. What people do while watching (play,
pause, Sync up...) is the controller's business (syncwatch/sync/controller.py);
it sends and receives those messages through this class.

Everything here runs on the app's main thread. The relay connection runs in a
background thread and only hands raw messages over (``sink``); ``handle()``
then opens and checks them here. ``tick()`` is called about ten times a second
for timers (rejoining, timeouts, pings). No Qt in here.
"""
from __future__ import annotations

import collections
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Callable, Deque, Dict, List, Optional, Tuple

from .. import PROTOCOL_VERSION, __version__
from ..net import protocol
from ..net.protocol import b64
from . import crypto
from .codes import RoomCode, same_relay
from .identity import Identity, PublicIdentity
from .store import RoomStore

log = logging.getLogger("syncwatch.room")

# roles
NONE, HOST, JOINING, MEMBER = "none", "host", "joining", "member"
# phases while joining
CONNECTING, LOOKING, NOBODY, ASKING, REJOINING = "connecting", "looking", "nobody", "asking", "rejoining"

HELLO_EVERY_S = 3.0           # while looking for the host
HELLO_WHILE_ASKING_S = 10.0   # notices a restarted host (new challenge)
KNOCK_AGAIN_S = 60.0          # while waiting for the host's OK, so the request doesn't expire
NOBODY_AFTER_S = 8.0
MEMBER_TIMEOUT_S = 20.0       # the host removes members it hasn't heard from for this long
HOST_LOST_S = 15.0
EPOCH_GRACE_S = 5.0           # the previous room key still opens messages for this long after a rekey
MEMBERS_EVERY_S = 30.0
CARD_MIN_GAP_S = 1.0
WAITING_EXPIRES_S = 5 * 60
MAX_WAITING = 25            # more waiting at once is spam: they get "the host is busy"
KNOCKS_PER_MIN = 6
PING_FAST_S, PING_SLOW_S, FAST_PING_FOR_S = 2.0, 10.0, 20.0
CLOCK_SAMPLES = 8

HOST_ONLY = {"members", "close", "state", "cmd", "reject", "sync_prepare", "sync_go", "sync_abort", "pong"}
MEMBERS_ONLY = {"req", "sync_ready", "ping", "bye"}     # only for the host (statuses everyone sees)
FRIEND_KINDS = {"friend_req", "friend_ok", "friend_no"}


@dataclass
class Member:
    key: PublicIdentity
    name: str
    is_host: bool = False
    is_me: bool = False
    joined: float = 0.0       # (monotonic clock)
    last_seen: float = 0.0
    session: str = ""

    @property
    def id(self) -> bytes:
        return self.key.key_id


@dataclass
class Knock:
    key: PublicIdentity
    name: str
    nonce: bytes
    app: str
    at: float
    friend: bool = False
    friend_name: str = ""     # the name you know them by, if they're a friend

    @property
    def id(self) -> bytes:
        return self.key.key_id


@dataclass
class _Clock:
    samples: Deque[Tuple[float, float]] = field(default_factory=lambda: collections.deque(maxlen=CLOCK_SAMPLES))

    def add(self, rtt_ms: float, offset_ms: float) -> None:
        self.samples.append((rtt_ms, offset_ms))

    @property
    def ready(self) -> bool:
        return bool(self.samples)

    def offset(self) -> float:
        return min(self.samples)[1] if self.samples else 0.0

    def rtt(self) -> float:
        return min(self.samples)[0] if self.samples else 0.0


class RoomListener:
    """What the session tells the controller. All methods are optional to override."""

    def room_changed(self, what: str, **info) -> None:
        pass

    def room_problem(self, code: str, technical: str = "", **params) -> None:
        pass

    def room_message(self, member: Member, body: Dict) -> None:
        pass

    def friend_message(self, sender: PublicIdentity, member: Optional[Member], body: Dict) -> None:
        pass


class RoomSession:
    def __init__(self, identity: Identity, store: RoomStore, settings, listener: RoomListener,
                 sink: Callable[[Dict], None], relay_for: Callable[[str], object],
                 default_relay: Callable[[], str], clock: Callable[[], float] = time.monotonic):
        self.me = identity
        self.store = store
        self.settings = settings
        self.listener = listener
        self.sink = sink
        self.relay_for = relay_for              # address -> net.relay.Relay
        self.default_relay = default_relay      # () -> the relay address in use ("" = none set up)
        self.clock = clock
        self.session_id = os.urandom(8).hex()
        self.protocol = PROTOCOL_VERSION        # (tests pretend to be another version by changing this)
        self._seq = 0
        self._gen = 0
        self.channel = None
        self.channel_state = ""
        self.channel_error = None
        self._reset()

    def _reset(self) -> None:
        self.role = NONE
        self.phase = ""
        self.code: Optional[RoomCode] = None
        self.relay_address = ""
        self.keys: Optional[crypto.RoomKeys] = None
        self.members: Dict[bytes, Member] = {}
        self.waiting: Dict[bytes, Knock] = {}
        self.host: Optional[Member] = None
        self.room_name = ""
        self.epochs: Dict[bytes, bytes] = {}
        self.epoch_id: Optional[bytes] = None
        self.epoch_no = 0
        self._old_epochs: Dict[bytes, float] = {}
        self.guard = crypto.ReplayGuard()
        self.challenge = b""
        self._used_nonces: Deque[bytes] = collections.deque(maxlen=512)
        self._knock_times: Dict[bytes, Deque[float]] = {}
        self._last_card = -1e9
        self._last_members = 0.0
        self._last_recent = 0.0
        # joining
        self._host_card: Optional[Tuple[PublicIdentity, str, bytes]] = None   # (key, name, challenge)
        self._my_nonce = b""
        self._knocked_challenge = b""
        self._knock_time = 0.0
        self._last_hello = -1e9
        self._looking_since = 0.0
        self._nobody_told = False
        self._host_seen = 0.0
        self._host_lost = False
        self._joined_at = 0.0
        self._next_ping = 0.0
        self._pings: Dict[float, float] = {}
        self._clock = _Clock()
        self._warned_impostors: set = set()
        self._version_told = False
        self.moving = False

    # ================================================================ properties
    @property
    def active(self) -> bool:
        return self.role != NONE

    @property
    def in_room(self) -> bool:
        return self.role in (HOST, MEMBER)

    @property
    def is_host(self) -> bool:
        return self.role == HOST

    def my_name(self) -> str:
        return (self.settings.get("profile.name") or "Someone").strip()[:32] or "Someone"

    def others(self) -> List[Member]:
        return [m for m in self.members.values() if not m.is_me]

    def member(self, key_id: bytes) -> Optional[Member]:
        return self.members.get(key_id)

    def host_clock_ms(self, now: Optional[float] = None) -> int:
        """The room's clock (the host's), in ms. Members estimate it from pings."""
        local = (self.clock() if now is None else now) * 1000.0
        if self.role == HOST:
            return int(local)
        return int(local + self._clock.offset())

    @property
    def clock_ready(self) -> bool:
        return self.role == HOST or self._clock.ready

    def rtt_ms(self) -> float:
        return self._clock.rtt()

    def local_from_host(self, host_ms: float) -> float:
        """A moment on the host's clock -> the same moment on my clock (seconds)."""
        if self.role == HOST:
            return host_ms / 1000.0
        return (host_ms - self._clock.offset()) / 1000.0

    def _policy(self) -> str:
        policy = self.settings.get("room.policy")
        return policy if policy in protocol.POLICIES else "ask"

    def _max_people(self) -> int:
        return max(2, min(protocol.MAX_PEOPLE, int(self.settings.get("room.max_people"))))

    # ================================================================ starting and stopping
    def host_room(self) -> bool:
        """Open my room (the same code as last time, unless New code was pressed)."""
        address = self.default_relay()
        if not address:
            self.listener.room_problem("SW-401")
            return False
        self.stop(quiet=True, forget=False)
        self.relay_address = address
        self.code = self.store.my_code(self.me, self._code_relay(address))
        self.keys = crypto.room_keys(self.code)
        self.role = HOST
        self.challenge = os.urandom(16)
        self.room_name = f"{self.my_name()}'s room"
        me = Member(self.me.public, self.my_name(), is_host=True, is_me=True, joined=self.clock(),
                    last_seen=self.clock(), session=self.session_id)
        self.members = {me.id: me}
        self.host = me
        self.epoch_no = 0
        self._new_epoch()
        self._open_channel()
        log.info("hosting room %s on %s", self.keys.channel_id[:8], address)
        self.listener.room_changed("hosting", code=self.code, room=self.room_name)
        return True

    def join(self, code: RoomCode, moving: bool = False) -> bool:
        address = code.relay or self.default_relay()
        if not address:
            self.listener.room_problem("SW-401")
            return False
        if code.host_commit == self.me.public.key_id:
            # That's my own room: open it instead of knocking on my own door.
            if self.store.room_secret == code.secret:
                return self.host_room()
            self.listener.room_problem("SW-407", "that code is an old code of your own room")
            return False
        self.stop(quiet=True)
        self.relay_address = address
        self.code = code
        self.keys = crypto.room_keys(code)
        self.role = JOINING
        self.phase = CONNECTING
        self.moving = moving
        self._open_channel()
        self.listener.room_changed("joining", moving=moving)
        return True

    def stop(self, quiet: bool = False, forget: bool = True) -> None:
        """Leave the room, or close it if I'm the host.

        Closing my room on purpose also forgets who was just in it, so next time everyone
        asks again (``forget=False`` when the room only moves to a new code).
        """
        if self.role == HOST and self.keys is not None:
            self.send_room("close")
            if forget:
                self.store.forget_recent()
                self.store.save()
        elif self.role == MEMBER:
            self.send_room("bye")
        was = self.role
        self._close_channel()
        self._reset()
        if not quiet and was != NONE:
            self.listener.room_changed("closed_by_me" if was == HOST else "left")

    def _code_relay(self, address: str) -> str:
        """Room codes only mention the relay when it isn't the built-in one."""
        from ..net.relay_url import RELAY_URL
        return "" if same_relay(address, RELAY_URL) else address

    def _open_channel(self) -> None:
        self._gen += 1
        gen = self._gen
        relay = self.relay_for(self.relay_address)
        self.channel_state, self.channel_error = "connecting", None
        self.channel = relay.channel(
            self.keys.channel_id,
            on_message=lambda data: self.sink({"t": "_relay", "gen": gen, "data": data}),
            on_state=lambda state, error: self.sink({"t": "_relay_state", "gen": gen, "state": state,
                                                     "error": error}),
            name="room").start()

    def _close_channel(self) -> None:
        self._gen += 1
        if self.channel is not None:
            self.channel.close()
        self.channel = None
        self.channel_state, self.channel_error = "", None

    # ================================================================ sending
    def _body(self, kind: str, **fields) -> Dict:
        self._seq += 1
        return {"t": kind, "s": self.session_id, "n": self._seq, **fields}

    def _send(self, data: bytes) -> bool:
        return self.channel is not None and self.channel.send(data)

    def send_room(self, kind: str, **fields) -> bool:
        """To everyone in the room (encrypted with the current room key)."""
        if self.role not in (HOST, MEMBER) or self.epoch_id is None or self.keys is None:
            return False
        try:
            data = crypto.pack_room(self.keys.channel, self.epoch_id, self.epochs[self.epoch_id], self.me,
                                    self._body(kind, **fields))
        except ValueError as exc:
            log.warning("not sending %s: %s", kind, exc)
            return False
        return self._send(data)

    def _send_lobby(self, kind: str, **fields) -> bool:
        if self.keys is None:
            return False
        return self._send(crypto.pack_lobby(self.keys.channel, self.keys.lobby_key, self.me,
                                            self._body(kind, **fields)))

    def _send_direct(self, to: PublicIdentity, kind: str, **fields) -> bool:
        if self.keys is None:
            return False
        return self._send(crypto.pack_direct(self.keys.channel, to, self.me, self._body(kind, **fields)))

    def send_to(self, key_id: bytes, kind: str, **fields) -> bool:
        """A direct message to one person in the room (e.g. a friend request)."""
        member = self.members.get(key_id)
        if member is None or member.is_me or kind not in FRIEND_KINDS:
            return False
        return self._send_direct(member.key, kind, **fields)

    # ================================================================ receiving
    def handle_event(self, event: Dict) -> None:
        if event.get("gen") != self._gen:
            return  # from a connection that has been closed since
        if event["t"] == "_relay_state":
            self._on_channel_state(event["state"], event.get("error"))
        else:
            self._on_data(event["data"])

    def _on_channel_state(self, state: str, error) -> None:
        self.channel_state, self.channel_error = state, error
        self.listener.room_changed("connection", state=state, error=error)
        if state != "connected":
            return
        if self.role == HOST:
            self._broadcast_card()
        elif self.role == JOINING:
            if self.phase == CONNECTING:
                self.phase = LOOKING
                self._looking_since = self.clock()
            self._hello()

    def _on_data(self, data: bytes) -> None:
        if self.keys is None:
            return
        try:
            kind, field_ = crypto.peek(data)
            if kind == crypto.LOBBY:
                opened, table = crypto.open_lobby(data, self.keys.channel, self.keys.lobby_key), protocol.LOBBY
            elif kind == crypto.DIRECT:
                if field_ != self.me.public.key_id:
                    return
                opened, table = crypto.open_direct(data, self.keys.channel, self.me), protocol.DIRECT
            else:
                opened, table = crypto.open_room(data, self.keys.channel, self._usable_epochs()), protocol.ROOM
            body = protocol.validate(table, opened.body)
        except (crypto.BadEnvelope, protocol.BadMessage) as exc:
            log.debug("dropped a message: %s", exc)
            return
        if body is None or opened.sender == self.me.public.sign_pub:
            return
        if not self.guard.accept(opened.sender, body["s"], body["n"]):
            log.debug("dropped a replayed %s", body["t"])
            return
        if kind == crypto.LOBBY:
            self._on_lobby(opened.sender, body)
        elif kind == crypto.DIRECT:
            self._on_direct(opened.sender, body)
        else:
            self._on_room(opened.sender, body)

    def _usable_epochs(self) -> Dict[bytes, bytes]:
        now = self.clock()
        return {eid: key for eid, key in self.epochs.items()
                if eid == self.epoch_id or self._old_epochs.get(eid, 0) > now}

    # ---------------------------------------------------------------- lobby
    def _on_lobby(self, sender: bytes, body: Dict) -> None:
        kind = body["t"]
        if kind == "hello" and self.role == HOST:
            if self.clock() - self._last_card >= CARD_MIN_GAP_S:
                self._broadcast_card()
        elif kind == "card" and self.role in (JOINING, MEMBER):
            self._on_card(sender, body)

    def _broadcast_card(self) -> None:
        self._last_card = self.clock()
        self._send_lobby("card", name=self.my_name(), dh=b64(self.me.public.dh_pub), app=__version__,
                         v=self.protocol, challenge=b64(self.challenge), people=len(self.members),
                         max=self._max_people(), policy=self._policy())

    def _on_card(self, sender: bytes, body: Dict) -> None:
        key = PublicIdentity(sender, body["dh"])
        if self.code is None or key.key_id != self.code.host_commit:
            if sender not in self._warned_impostors:
                self._warned_impostors.add(sender)
                log.warning("someone who isn't the host sent a room card (%s)", key.short_id)
                self.listener.room_problem("SW-408", f"card signed by {key.short_id}, not the room's host")
            return
        if body["v"] != self.protocol:
            if not self._version_told:
                self._version_told = True
                self.listener.room_problem("SW-413", f"host protocol {body['v']}, mine {self.protocol}",
                                           theirs=body["app"], mine=__version__)
            return
        self._host_card = (key, body["name"], body["challenge"])
        if self.role == JOINING:
            if self.phase in (CONNECTING, LOOKING, NOBODY, REJOINING) or \
                    (self.phase == ASKING and body["challenge"] != self._knocked_challenge):
                self._knock()
        elif self.role == MEMBER and (self._host_lost or body["challenge"] != self._knocked_challenge):
            # The host restarted SyncWatch, or dropped us after a long disconnect: our room key is
            # gone, so knock again (people who were just in the room get straight back in).
            self.role, self.phase = JOINING, REJOINING
            self.listener.room_changed("rejoining")
            self._knock()

    def _hello(self) -> None:
        self._last_hello = self.clock()
        self._send_lobby("hello")

    def _knock(self) -> None:
        assert self._host_card is not None
        key, name, challenge = self._host_card
        self._my_nonce = os.urandom(16)
        self._knocked_challenge = challenge
        self._knock_time = self.clock()
        was = self.phase
        self.phase = REJOINING if was == REJOINING else ASKING
        self._send_direct(key, "knock", challenge=b64(challenge), nonce=b64(self._my_nonce), name=self.my_name(),
                          dh=b64(self.me.public.dh_pub), app=__version__, v=self.protocol)
        if was not in (REJOINING, ASKING):
            self.listener.room_changed("asking", host=name)

    # ---------------------------------------------------------------- direct
    def _on_direct(self, sender: bytes, body: Dict) -> None:
        kind = body["t"]
        if kind in FRIEND_KINDS:
            member = self._member_by_sign(sender)
            if member is not None and self.in_room:
                self.listener.friend_message(member.key, member, body)
            return
        if self.role == HOST:
            if kind == "knock":
                self._on_knock(sender, body)
            return
        # Everything else must come from the room's host.
        if self._host_card is None or sender != self._host_card[0].sign_pub:
            return
        if kind == "welcome" and self.role == JOINING and body["nonce"] == self._my_nonce:
            self._on_welcome(body)
        elif kind == "deny" and self.role == JOINING and body["nonce"] == self._my_nonce:
            reason = body["reason"]
            self._close_channel()
            self._reset()
            self.listener.room_changed("denied", reason=reason)
        elif kind == "rekey" and self.role == MEMBER and body["no"] > self.epoch_no:
            self._switch_epoch(body["epoch"], body["key"], body["no"])
            self._apply_members(body["members"])
        elif kind == "kicked" and self.role in (MEMBER, JOINING):
            ban = bool(body.get("ban"))
            self._close_channel()
            self._reset()
            self.listener.room_changed("kicked", ban=ban)
        elif kind == "moved" and self.role == MEMBER:
            from .codes import CodeError, parse
            try:
                code = parse(body["code"])
            except CodeError:
                return
            if code.host_commit != self._host_card[0].key_id:
                return  # a room has to stay with its host
            self.listener.room_changed("moved")
            self.join(code, moving=True)

    def _on_welcome(self, body: Dict) -> None:
        host_key, host_name, _ = self._host_card
        rejoined = self.phase == REJOINING
        self.role, self.phase = MEMBER, ""
        self.epochs = {}
        self._old_epochs = {}
        self.epoch_no = 0
        self._switch_epoch(body["epoch"], body["key"], body["no"])
        self.room_name = body["room"]
        now = self.clock()
        self._host_seen = now
        self._host_lost = False
        self._joined_at = now
        self._next_ping = now
        me = Member(self.me.public, self.my_name(), is_me=True, joined=now, last_seen=now, session=self.session_id)
        self.members = {me.id: me}
        self.host = None
        self._apply_members(body["members"], announce=False)
        if self.host is None or self.host.key != host_key:
            log.warning("welcome without the host in the member list")
        self.listener.room_changed("joined", room=self.room_name, host=host_name, moving=self.moving,
                                   rejoined=rejoined)
        self.moving = False

    def _switch_epoch(self, epoch_id: bytes, key: bytes, number: int) -> None:
        if self.epoch_id is not None:
            self._old_epochs[self.epoch_id] = self.clock() + EPOCH_GRACE_S
        self.epochs[epoch_id] = key
        self.epoch_id = epoch_id
        self.epoch_no = number

    def _apply_members(self, entries: List[Dict], announce: bool = True) -> None:
        now = self.clock()
        new: Dict[bytes, Member] = {}
        for entry in entries:
            try:
                key = PublicIdentity(entry["id"], entry["dh"])
            except ValueError:
                continue
            if key == self.me.public:
                new[key.key_id] = self.members.get(key.key_id) or Member(key, self.my_name(), is_me=True, joined=now)
                continue
            old = self.members.get(key.key_id)
            member = old if old is not None and old.key == key else Member(key, entry["name"], joined=now,
                                                                            last_seen=now)
            member.name = entry["name"]
            member.is_host = bool(entry.get("host"))
            if member.is_host and (self._host_card is None or member.key != self._host_card[0]):
                member.is_host = False  # only the host whose keys match the code can be the host
            new[key.key_id] = member
        joined = [m for k, m in new.items() if k not in self.members and not m.is_me]
        left = [m for k, m in self.members.items() if k not in new and not m.is_me]
        self.members = new
        self.host = next((m for m in new.values() if m.is_host), None)
        if announce:
            for member in joined:
                self.listener.room_changed("member_joined", member=member)
            for member in left:
                self.listener.room_changed("member_left", member=member, reason="")
        if joined or left or not announce:
            self.listener.room_changed("members")

    # ---------------------------------------------------------------- room
    def _member_by_sign(self, sender: bytes) -> Optional[Member]:
        for member in self.members.values():
            if member.key.sign_pub == sender:
                return member
        return None

    def _on_room(self, sender: bytes, body: Dict) -> None:
        member = self._member_by_sign(sender)
        if member is None or member.is_me:
            return
        now = self.clock()
        member.last_seen = now
        member.session = body["s"]
        kind = body["t"]
        if self.role == HOST:
            if kind in HOST_ONLY:
                return  # only I may send those
            if kind == "bye":
                self._remove(member, "left")
            elif kind == "ping":
                self.send_room("pong", id=body["id"], to=b64(member.id), at=self.host_clock_ms(now))
            else:
                self.listener.room_message(member, body)
            return
        if self.role != MEMBER:
            return
        if member.is_host:
            self._host_seen = now
            if self._host_lost:
                self._host_lost = False
                self.listener.room_changed("host_back")
        elif kind in HOST_ONLY:
            return  # a member pretending to be the host
        if kind in MEMBERS_ONLY:
            return  # members' pings, statuses etc. are for the host
        if kind == "members":
            if body["no"] >= self.epoch_no:
                self._apply_members(body["list"])
        elif kind == "close":
            name = member.name
            self._close_channel()
            self._reset()
            self.listener.room_changed("closed", host=name)
        elif kind == "pong":
            if body["to"] == self.me.public.key_id:
                sent = self._pings.pop(body["id"], None)
                if sent is not None:
                    rtt = now * 1000.0 - body["id"]
                    if 0 <= rtt < 10_000:
                        self._clock.add(rtt, body["at"] + rtt / 2.0 - now * 1000.0)
        else:
            self.listener.room_message(member, body)

    # ================================================================ the host's decisions
    def _on_knock(self, sender: bytes, body: Dict) -> None:
        now = self.clock()
        if body["challenge"] != self.challenge or body["nonce"] in self._used_nonces:
            return
        try:
            key = PublicIdentity(sender, body["dh"])
        except ValueError:
            return
        times = self._knock_times.setdefault(key.key_id, collections.deque(maxlen=KNOCKS_PER_MIN))
        if len(times) == KNOCKS_PER_MIN and now - times[0] < 60:
            return  # knocking far too often: ignore
        times.append(now)
        self._used_nonces.append(body["nonce"])
        friend = self.store.friends.get(key.key_id)
        is_friend = friend is not None and friend.key == key
        knock = Knock(key, body["name"] or "Someone", body["nonce"], body["app"], now, is_friend,
                      friend.name if is_friend else "")
        if body["v"] != self.protocol:
            self._deny(knock, "version")
            return
        decision = self._decide(knock)
        if decision == "admit":
            self._admit(knock)
        elif decision == "ask":
            if knock.id not in self.waiting and len(self.waiting) >= MAX_WAITING:
                self._deny(knock, "busy")
                return
            new = knock.id not in self.waiting
            self.waiting[knock.id] = knock
            self.listener.room_changed("waiting", knock=knock if new else None)
        else:
            self._deny(knock, decision)

    def _decide(self, knock: Knock) -> str:
        key = knock.key
        if self.store.is_blocked(key):
            return "denied"
        current = self.members.get(key.key_id)
        if current is not None and current.key == key:
            return "admit"  # already in (their SyncWatch restarted)
        if len(self.members) >= self._max_people():
            return "full"
        if self.settings.get("room.rejoin") and self.store.was_recently_in_room(key):
            return "admit"
        policy = self._policy()
        if policy == "open" or (policy == "friends_direct" and knock.friend):
            return "admit"
        if policy == "friends_only" and not knock.friend:
            return "friends_only"
        return "ask"

    def approve(self, key_id: bytes) -> bool:
        return self._approve([key_id]) == 1

    def approve_all(self) -> int:
        return self._approve(list(self.waiting))

    def _approve(self, key_ids: List[bytes]) -> int:
        """Let these people in, all with one new room key."""
        if self.role != HOST:
            return 0
        admitted: List[Knock] = []
        for key_id in key_ids:
            knock = self.waiting.pop(key_id, None)
            if knock is None:
                continue
            if len(self.members) + len(admitted) >= self._max_people():
                self._deny(knock, "full")
                self.listener.room_problem("SW-410", "the room is full")
                continue
            admitted.append(knock)
        if admitted:
            self._admit(*admitted)
        self.listener.room_changed("waiting", knock=None)
        return len(admitted)

    def deny(self, key_id: bytes, block: bool = False) -> bool:
        knock = self.waiting.pop(key_id, None)
        if knock is None:
            return False
        self._deny(knock, "denied")
        if block:
            self.store.block(knock.key, knock.name)
        self.listener.room_changed("waiting", knock=None)
        return True

    def _deny(self, knock: Knock, reason: str) -> None:
        self._send_direct(knock.key, "deny", nonce=b64(knock.nonce), reason=reason)

    def _admit(self, *knocks: Knock) -> None:
        now = self.clock()
        joined = []
        for knock in knocks:
            existing = self.members.get(knock.id)
            member = Member(knock.key, knock.name, joined=existing.joined if existing else now, last_seen=now)
            self.members[knock.id] = member
            if existing is None:
                joined.append(member)
        self.store.remember_in_room([k.key for k in knocks])
        self._new_epoch(newcomers={k.id: k for k in knocks})
        for member in joined:
            self.listener.room_changed("member_joined", member=member)
        self.listener.room_changed("members")

    def kick(self, key_id: bytes, ban: bool = False) -> bool:
        member = self.members.get(key_id)
        if self.role != HOST or member is None or member.is_me:
            return False
        self._send_direct(member.key, "kicked", ban=ban)
        self.store.forget_recent(key_id)
        if ban:
            self.store.block(member.key, member.name)
        self._remove(member, "banned" if ban else "removed")
        return True

    def _remove(self, member: Member, reason: str) -> None:
        if self.members.pop(member.id, None) is None:
            return
        self.guard.forget(member.key.sign_pub)
        if reason in ("left", "removed", "banned"):
            self.store.forget_recent(member.id)  # (after a timeout they may come straight back)
        self._new_epoch()
        self.listener.room_changed("member_left", member=member, reason=reason)
        self.listener.room_changed("members")

    def _new_epoch(self, newcomers: Optional[Dict[bytes, Knock]] = None) -> None:
        """A fresh room key, handed to everyone still in the room (and the newcomers)."""
        if self.epoch_id is not None:
            self._old_epochs[self.epoch_id] = self.clock() + EPOCH_GRACE_S
        epoch_id, key = crypto.new_epoch()
        self.epochs[epoch_id] = key
        self.epoch_id = epoch_id
        self.epoch_no += 1
        entries = self._member_list()
        for member in self.others():
            newcomer = (newcomers or {}).get(member.id)
            if newcomer is not None:
                self._send_direct(member.key, "welcome", nonce=b64(newcomer.nonce), epoch=b64(epoch_id),
                                  key=b64(key), no=self.epoch_no, members=entries, room=self.room_name)
            else:
                self._send_direct(member.key, "rekey", epoch=b64(epoch_id), key=b64(key), no=self.epoch_no,
                                  members=entries)
        self._last_members = self.clock()

    def _member_list(self) -> List[Dict]:
        return [{"id": b64(m.key.sign_pub), "dh": b64(m.key.dh_pub), "name": m.name, "host": m.is_host}
                for m in self.members.values()]

    def new_code(self) -> Optional[RoomCode]:
        """Make a new room code. People in the room follow automatically; the old code stops working."""
        if self.role != HOST:
            self.store.new_code(self.me, "")
            return None
        old_members = self.others()
        self.store.remember_in_room([m.key for m in old_members])
        address = self.relay_address
        code = self.store.new_code(self.me, self._code_relay(address))
        for member in old_members:
            self._send_direct(member.key, "moved", code=code.text())
        self.stop(quiet=True, forget=False)
        self.host_room()
        return self.code

    def rename(self) -> None:
        """My name changed: tell the room (members get it from the next member list)."""
        me = self.members.get(self.me.public.key_id)
        if me is not None:
            me.name = self.my_name()
        if self.role == HOST:
            self.room_name = f"{self.my_name()}'s room"
            self.send_room("members", no=self.epoch_no, list=self._member_list())

    def rename_member(self, member: Member, name: str) -> None:
        """A member's profile message had a new name (the host passes it on in the member list)."""
        if self.role == HOST and member.name != name:
            member.name = name
            self.send_room("members", no=self.epoch_no, list=self._member_list())

    # ================================================================ timers
    def tick(self) -> None:
        now = self.clock()
        for epoch_id in [e for e, until in self._old_epochs.items() if until <= now]:
            del self._old_epochs[epoch_id]
            if epoch_id != self.epoch_id:
                self.epochs.pop(epoch_id, None)
        if self.role == HOST:
            self._tick_host(now)
        elif self.role == JOINING:
            self._tick_joining(now)
        elif self.role == MEMBER:
            self._tick_member(now)

    def _tick_host(self, now: float) -> None:
        for member in self.others():
            if now - member.last_seen > MEMBER_TIMEOUT_S:
                log.info("%s timed out", member.name)
                self._remove(member, "timeout")
        for key_id in [k for k, knock in self.waiting.items() if now - knock.at > WAITING_EXPIRES_S]:
            del self.waiting[key_id]
            self.listener.room_changed("waiting", knock=None)
        if self.channel_state == "connected" and now - self._last_members > MEMBERS_EVERY_S:
            self._last_members = now
            self.send_room("members", no=self.epoch_no, list=self._member_list())
        if now - self._last_recent > 30:
            self._last_recent = now
            others = [m.key for m in self.others()]
            if others:
                self.store.remember_in_room(others)
                self.store.save()

    def _tick_joining(self, now: float) -> None:
        if self.channel_state != "connected":
            return
        every = HELLO_WHILE_ASKING_S if self.phase == ASKING else HELLO_EVERY_S
        if now - self._last_hello >= every:
            self._hello()
        if self.phase == ASKING and self._host_card is not None and now - self._knock_time >= KNOCK_AGAIN_S:
            self._knock()
        if self.phase == LOOKING and not self._nobody_told and now - self._looking_since > NOBODY_AFTER_S:
            self._nobody_told = True
            self.phase = NOBODY
            self.listener.room_changed("nobody")

    def _tick_member(self, now: float) -> None:
        if not self._host_lost and now - self._host_seen > HOST_LOST_S:
            self._host_lost = True
            self.listener.room_changed("host_lost")
        if self._host_lost and self.channel_state == "connected" and now - self._last_hello >= HELLO_EVERY_S:
            self._hello()  # if the host is back with a new key, its card lets us knock again
        if now >= self._next_ping and self.channel_state == "connected":
            fast = now - self._joined_at < FAST_PING_FOR_S
            self._next_ping = now + (PING_FAST_S if fast else PING_SLOW_S)
            ping_id = round(now * 1000.0, 3)
            self._pings[ping_id] = now
            if len(self._pings) > 20:
                self._pings.pop(next(iter(self._pings)))
            self.send_room("ping", id=ping_id)
