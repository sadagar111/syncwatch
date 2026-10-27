"""Watching in sync: what happens when anyone presses a button.

Rules (agreed for step 1):
* Nothing is corrected automatically while the film plays. Jumping to fix a
  small gap is what caused the stutter in version 0.1. Instead the gap is
  measured and shown, and people fix it when *they* want:
    - "Sync me"   jumps only yourself to where the others are (no one else is disturbed);
    - "Sync up"   pauses everyone, lines everyone up on the host, then resumes together
                  after a short countdown.
* The host's app decides. Friends' presses are *requests* that the host's app
  checks (cooldowns, "only the host controls") and then broadcasts. The host
  has no cooldowns. Because the check happens on the host's PC, editing your
  own copy of the app can't get around it.
* Play and pause can't be spammed (cooldown); skipping can (it's sent once,
  shortly after you stop pressing).
* "Watching on your own": your buttons only affect you and you ignore the
  room until you press Sync me / Rejoin. When the host has "only I control"
  on, friends' buttons automatically work this way.

This module has no Qt in it. The UI gives it a player, calls ``tick()`` about
ten times a second, passes it network events with ``handle()``, and asks it
for the current state to draw.
"""
from __future__ import annotations

import base64
import collections
import logging
import statistics
import time
from dataclasses import dataclass, field
from typing import Callable, Deque, Dict, Optional

from .. import PROTOCOL_VERSION, __version__
from ..net.link import Link

log = logging.getLogger("syncwatch.sync")

HEARTBEAT_S = 1.0
GAP_SAMPLES = 5
SEEK_RATE_PER_S = 5          # host accepts at most this many seek requests per second
PAUSE_ALIGN_MS = 150         # on pause, jump to the exact frame if further apart than this
PLAY_ALIGN_MS = 300          # on play, jump first if further apart than this
READY_TOLERANCE_MS = 500
MATCH_TOLERANCE_MS = 1000    # films whose lengths differ by less than this count as the same


def fmt_time(ms: float) -> str:
    seconds = max(0, int(ms // 1000))
    return f"{seconds // 3600}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"


@dataclass
class Peer:
    name: str = "Friend"
    avatar: Optional[bytes] = None
    film: Optional[Dict] = None
    is_host: bool = False
    pos: int = 0
    playing: bool = False
    separate: bool = False
    ready: bool = True
    received_at: float = 0.0
    known: bool = False          # received at least one state


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
    peer_ready: bool = False
    go_at: Optional[float] = None


class Controller:
    def __init__(self, player, ui, settings, event_sink: Callable[[Dict], None],
                 clock: Callable[[], float] = time.monotonic):
        self.p = player
        self.ui = ui
        self.settings = settings
        self.clock = clock
        self.link = Link(event_sink, self.hello_message)
        self.name = "Me"
        self.avatar_b64: Optional[str] = None
        self.film: Optional[Dict] = None
        self.peer: Optional[Peer] = None
        self.attached = True
        self.rules = Rules()               # as decided by the host (or by me when I host)
        self.gaps: Deque[float] = collections.deque(maxlen=GAP_SAMPLES)
        self.match_state = ""              # last film comparison result shown
        self._next_heartbeat = 0.0
        self._pending_seek_at: Optional[float] = None
        self._my_cooldown_until: Dict[str, float] = {"pp": 0.0, "sync": 0.0}
        self._friend_last: Dict[str, float] = {"pp": -1e9, "sync": -1e9}
        self._seek_times: Deque[float] = collections.deque(maxlen=SEEK_RATE_PER_S)
        self._host_sync: Optional[_HostSync] = None
        self._client_sync: Optional[Dict] = None
        self._play_at: Optional[float] = None
        self._sync_counter = 0
        self._align_on_next_state = False

    # ================================================================ setup
    def set_profile(self, name: str, avatar_png: Optional[bytes]) -> None:
        self.name = name or "Me"
        self.avatar_b64 = base64.b64encode(avatar_png).decode("ascii") if avatar_png else None
        if self.connected:
            message = {"t": "profile", "name": self.name}
            if self.avatar_b64:
                message["avatar"] = self.avatar_b64
            self.link.send(message)

    def set_film(self, name: str, size: int, duration_ms: int) -> None:
        film = {"name": name[:255], "size": int(size), "dur": max(0, int(duration_ms))}
        if film == self.film:
            return
        self.film = film
        self.link.send({"t": "film", "film": film})
        self._compare_films()

    def hello_message(self) -> Dict:
        message = {"t": "hello", "v": PROTOCOL_VERSION, "app": __version__, "name": self.name}
        if self.avatar_b64:
            message["avatar"] = self.avatar_b64
        if self.film:
            message["film"] = self.film
        return message

    # ================================================================ state for the UI
    @property
    def connected(self) -> bool:
        return self.link.connected and self.peer is not None

    @property
    def is_host(self) -> bool:
        return self.link.is_host

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

    def peer_estimate(self, now: Optional[float] = None) -> Optional[float]:
        """Where the friend's film is right now (ms), estimated from their last report."""
        peer = self.peer
        if peer is None or not peer.known:
            return None
        now = self.clock() if now is None else now
        if not peer.playing:
            return float(peer.pos)
        estimate = peer.pos + (now - peer.received_at) * 1000.0
        if self.settings.get("sync.compensate_latency"):
            estimate += self.link.rtt * 500.0  # their report took half a round trip to arrive
        return estimate

    def gap_ms(self) -> Optional[float]:
        """How far my film is ahead (+) or behind (-) the friend's, smoothed. None if unknown."""
        if not self.gaps:
            return None
        return statistics.median(self.gaps)

    @property
    def syncing(self) -> bool:
        return self._host_sync is not None or self._client_sync is not None or self._play_at is not None

    # ================================================================ connecting
    def host(self) -> bool:
        self._reset_peer()
        return self.link.host(int(self.settings.get("sync.port")))

    def join(self, address: str) -> bool:
        self._reset_peer()
        ok = self.link.join(address, int(self.settings.get("sync.port")))
        if ok:
            self.settings.set("sync.last_address", address.strip())
        return ok

    def leave(self) -> None:
        self.link.close()
        self._reset_peer()
        self.ui.status("Not connected", False)
        self.ui.changed()

    def _reset_peer(self) -> None:
        self.peer = None
        self.gaps.clear()
        self.match_state = ""
        self._host_sync = None
        self._client_sync = None
        self._play_at = None
        self.attached = True

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
            self._broadcast_cmd(action, by="")
            self.ui.activity(f"You {'paused' if action == 'pause' else 'pressed play'} at {fmt_time(self.p.position())}", "you")
            return
        left = self.cooldown_left("pp")
        if left > 0:
            self.ui.problem("SW-502", seconds=f"{left:.0f}")
            return
        self._my_cooldown_until["pp"] = self.clock() + self.rules.cd_pp
        self.link.send({"t": "req", "action": action, "pos": int(self.p.position())})
        self.ui.changed()

    def skip(self, delta_ms: int) -> None:
        self.seek(self.p.position() + delta_ms)

    def seek(self, ms: float) -> None:
        """Jump locally at once; tell the others once the skipping stops (debounced)."""
        self._cancel_countdown_if_local()
        self.p.seek(self._clamp(ms))
        if not self.connected:
            return
        if not self.controls_room():
            self._go_separate("seek")
            return
        debounce = int(self.settings.get("sync.seek_debounce_ms")) / 1000.0
        self._pending_seek_at = self.clock() + debounce

    def sync_up(self) -> None:
        if not self.connected:
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
            self.ui.problem("SW-502", seconds=f"{left:.0f}")
            return
        self._my_cooldown_until["sync"] = self.clock() + self.rules.cd_sync
        self.link.send({"t": "req", "action": "syncup"})
        self.ui.activity("You asked everyone to sync up", "you")
        self.ui.changed()

    def sync_me(self) -> None:
        """Jump only myself to where my friend is (and follow the room again)."""
        target = self.peer_estimate()
        if target is None or self.peer is None:
            self.ui.activity("Nothing to sync to yet", "info")
            return
        before = self.p.position()
        target = self._clamp(target)
        self.p.seek(target)
        if self.peer.playing and not self.p.is_playing():
            self.p.play()
        elif not self.peer.playing and self.p.is_playing():
            self.p.pause()
        was_separate = not self.attached
        self.attached = True
        self.gaps.clear()
        jump = (target - before) / 1000.0
        verb = "Rejoined the room" if was_separate else "Synced to " + self.peer.name
        self.ui.activity(f"{verb} ({jump:+.1f} s)", "you")
        self.ui.changed()

    def set_attached(self, attached: bool) -> None:
        if self.is_host:
            return  # the host is the reference everyone syncs to, so it can't "leave" the room
        if attached and not self.attached:
            self.sync_me()
        elif not attached and self.attached:
            self.attached = False
            self.ui.activity("You're watching on your own. Press Sync me to rejoin.", "you")
            self.ui.changed()

    # ================================================================ called by the UI timer
    def tick(self) -> None:
        now = self.clock()
        if self._pending_seek_at is not None and now >= self._pending_seek_at:
            self._pending_seek_at = None
            self._send_seek()
        self._advance_host_sync(now)
        self._advance_client_sync(now)
        if self._play_at is not None and now >= self._play_at:
            self._play_at = None
            self.p.play()
            self.ui.changed()
        if self.connected and now >= self._next_heartbeat:
            self._next_heartbeat = now + HEARTBEAT_S
            self._send_state()

    # ================================================================ network events
    def handle(self, message: Dict) -> None:
        kind = message.get("t")
        handler = getattr(self, f"_on_{kind}", None)
        if handler is None:
            log.debug("ignoring message %s", kind)
            return
        try:
            handler(message)
        finally:
            self.ui.changed()

    def _on__status(self, message: Dict) -> None:
        self.ui.status(message.get("text", ""), self.connected)

    def _on__problem(self, message: Dict) -> None:
        self.ui.problem(message.get("code", "SW-312"), message.get("technical", ""),
                        **message.get("params", {}))

    def _on__connected(self, message: Dict) -> None:
        self._reset_peer()
        self.peer = Peer(is_host=not message.get("host", False))
        self._align_on_next_state = not self.is_host
        self._next_heartbeat = 0.0

    def _on__disconnected(self, message: Dict) -> None:
        name = self.peer.name if self.peer else "Your friend"
        self._reset_peer()
        self.ui.activity(f"{name} disconnected ({message.get('reason', '')})", "warn")
        self.ui.status("Hosting. Waiting for your friend…" if self.is_host else "Not connected", False)

    def _on_hello(self, message: Dict) -> None:
        if self.peer is None:
            return
        self.peer.name = message["name"].strip() or "Friend"
        self.peer.avatar = self._decode_avatar(message.get("avatar"))
        self.peer.film = message.get("film")
        role = "you are hosting" if self.is_host else f"{self.peer.name} is hosting"
        self.ui.status(f"Connected to {self.peer.name} ({role})", True)
        self.ui.activity(f"{self.peer.name} joined" if self.is_host else f"Connected to {self.peer.name}", "friend")
        self._compare_films()
        if self.is_host:
            self._send_state()

    def _on_profile(self, message: Dict) -> None:
        if self.peer is not None:
            self.peer.name = message["name"].strip() or self.peer.name
            self.peer.avatar = self._decode_avatar(message.get("avatar"))

    def _on_film(self, message: Dict) -> None:
        if self.peer is not None:
            self.peer.film = message.get("film")
            self._compare_films()

    def _on_state(self, message: Dict) -> None:
        peer = self.peer
        if peer is None:
            return
        now = self.clock()
        peer.pos = message["pos"]
        peer.playing = message["playing"]
        peer.separate = message.get("separate", False)
        peer.ready = message.get("ready", True)
        peer.received_at = now
        peer.known = True
        rules = message.get("rules")
        if rules is not None and not self.is_host:
            old = self.rules.host_only
            self.rules = Rules(rules["host_only"], rules.get("cd_pp", 2.0), rules.get("cd_sync", 15.0))
            if self.rules.host_only != old:
                self.ui.activity("The host now controls playback alone" if self.rules.host_only
                                 else "Everyone can control playback again", "friend")
        if self._align_on_next_state and self.p.has_media():
            self._align_on_next_state = False
            self.sync_me()
            return
        estimate = self.peer_estimate(now)
        if (estimate is not None and self.p.has_media() and self.attached and not peer.separate
                and not self.syncing and self._pending_seek_at is None):
            self.gaps.append(self.p.position() - estimate)

    def _on_req(self, message: Dict) -> None:
        if not self.is_host or self.peer is None:
            return
        action = message["action"]
        rules = self.my_rules()
        now = self.clock()
        if rules.host_only:
            self.link.send({"t": "reject", "action": action, "reason": "host_only"})
            return
        if action in ("play", "pause"):
            wait = self._friend_last["pp"] + rules.cd_pp - now
            if wait > 0:
                self.link.send({"t": "reject", "action": action, "reason": "cooldown", "wait": wait})
                return
            self._friend_last["pp"] = now
            # Play always continues from the host's position, so everyone resumes together.
            self._local(action)
            self._broadcast_cmd(action, by=self.peer.name)
            self.ui.activity(f"{self.peer.name} {'paused' if action == 'pause' else 'pressed play'} at {fmt_time(self.p.position())}", "friend")
        elif action == "seek":
            if len(self._seek_times) == SEEK_RATE_PER_S and now - self._seek_times[0] < 1.0:
                return  # flood protection: silently drop
            self._seek_times.append(now)
            if "pos" not in message:
                return
            self.p.seek(self._clamp(message["pos"]))
            self._broadcast_cmd("seek", by=self.peer.name, position=message["pos"])
            self.ui.activity(f"{self.peer.name} jumped to {fmt_time(message['pos'])}", "friend")
        elif action == "syncup":
            if self._host_sync is not None:
                self.link.send({"t": "reject", "action": action, "reason": "busy"})
                return
            wait = self._friend_last["sync"] + rules.cd_sync - now
            if wait > 0:
                self.link.send({"t": "reject", "action": action, "reason": "cooldown", "wait": wait})
                return
            self._friend_last["sync"] = now
            self._start_host_sync(by=self.peer.name)

    def _on_cmd(self, message: Dict) -> None:
        if self.is_host or self.peer is None:
            return
        action, pos = message["action"], message["pos"]
        # A command also tells us exactly where the host is now, even if we ignore it below;
        # without this, "Sync me" could use host information up to a second old.
        self.peer.pos = pos
        self.peer.playing = message.get("playing", action == "play")
        self.peer.received_at = self.clock()
        self.peer.known = True
        # With two people, a command carrying "by" was requested by me; otherwise the host did it.
        mine = bool(message.get("by"))
        who = "You" if mine else self.peer.name
        if not self.attached:
            self.ui.activity(f"{who} {_verb(action)} {'to' if action == 'seek' else 'at'} {fmt_time(pos)} "
                             "(you're watching on your own)", "friend")
            return
        half_rtt = self.link.rtt * 500.0 if self.settings.get("sync.compensate_latency") else 0.0
        if action == "pause":
            self.p.pause()
            if abs(self.p.position() - pos) > PAUSE_ALIGN_MS:
                self.p.seek(pos)
        elif action == "play":
            target = pos + half_rtt
            if abs(self.p.position() - target) > PLAY_ALIGN_MS:
                self.p.seek(self._clamp(target))
            self.p.play()
        else:
            playing = message.get("playing", self.p.is_playing())
            target = self._clamp(pos + (half_rtt if playing else 0.0))
            if abs(self.p.position() - target) > PLAY_ALIGN_MS:
                self.p.seek(target)  # (my own skip comes back here: no second jump)
            if playing and not self.p.is_playing():
                self.p.play()
            elif not playing and self.p.is_playing():
                self.p.pause()
        self.gaps.clear()
        self.ui.activity(f"{who} {_verb(action)} {'to' if action == 'seek' else 'at'} {fmt_time(pos)}",
                         "you" if mine else "friend")

    def _on_reject(self, message: Dict) -> None:
        reason = message["reason"]
        if reason == "cooldown":
            wait = message.get("wait", 1.0)
            self._my_cooldown_until["pp" if message["action"] in ("play", "pause") else "sync"] = self.clock() + wait
            self.ui.problem("SW-502", seconds=f"{wait:.0f}")
        elif reason == "host_only":
            self.ui.problem("SW-504")
        else:
            self.ui.activity("A sync is already in progress", "info")

    def _on_sync_prepare(self, message: Dict) -> None:
        if self.is_host:
            return
        who = message.get("by") or (self.peer.name if self.peer else "The host")
        if not self.attached:
            self.ui.activity(f"{who} synced everyone up (you're watching on your own)", "friend")
            return
        self._play_at = None
        self.p.pause()
        self.p.seek(message["pos"])
        self._client_sync = {"id": message["id"], "pos": message["pos"], "sent": False}
        self.ui.activity(f"{who or 'The host'} is syncing everyone up…", "friend")

    def _on_sync_ready(self, message: Dict) -> None:
        sync = self._host_sync
        if self.is_host and sync is not None and message["id"] == sync.id:
            sync.peer_ready = True

    def _on_sync_go(self, message: Dict) -> None:
        client = self._client_sync
        if self.is_host or client is None or message["id"] != client["id"]:
            return
        self._client_sync = None
        delay = message["delay"] / 1000.0
        half_rtt = self.link.rtt / 2.0 if self.settings.get("sync.compensate_latency") else 0.0
        self._play_at = self.clock() + max(0.0, delay - half_rtt)
        self.ui.countdown(int(max(0.0, delay - half_rtt) * 1000))
        self.gaps.clear()

    def _on_sync_abort(self, message: Dict) -> None:
        if self._client_sync is not None and message["id"] == self._client_sync["id"]:
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
            reason = ("The host controls playback, so your" if self.effective_rules().host_only
                      else "Your")
            self.ui.activity(f"{reason} buttons now work just for you. Press Sync me to rejoin.", "you")
            self.gaps.clear()
            self.ui.changed()

    def _cancel_countdown_if_local(self) -> None:
        if self._play_at is not None and (self.is_host or not self.connected):
            self._play_at = None
            self._host_sync = None

    def _broadcast_cmd(self, action: str, by: str, position: Optional[float] = None) -> None:
        pos = int(self.p.position() if position is None else position)
        message = {"t": "cmd", "action": action, "pos": max(0, pos), "playing": self.p.is_playing()}
        if by:
            message["by"] = by
        self.link.send(message)
        self.gaps.clear()

    def _send_seek(self) -> None:
        if not self.controls_room():
            return
        if self.is_host:
            self._broadcast_cmd("seek", by="")
            self.ui.activity(f"You jumped to {fmt_time(self.p.position())}", "you")
        else:
            self.link.send({"t": "req", "action": "seek", "pos": max(0, int(self.p.position()))})

    def _send_state(self) -> None:
        message = {"t": "state", "pos": max(0, int(self.p.position())), "playing": self.p.is_playing(),
                   "separate": not self.attached, "ready": bool(self.p.is_ready())}
        if self.is_host:
            rules = self.my_rules()
            message["rules"] = {"host_only": rules.host_only, "cd_pp": rules.cd_pp, "cd_sync": rules.cd_sync}
        self.link.send(message)

    def _start_host_sync(self, by: str) -> None:
        if self._host_sync is not None:
            return
        self._sync_counter += 1
        pos = int(self.p.position())
        self.p.pause()
        self.p.seek(pos)
        timeout = float(self.settings.get("sync.ready_timeout_s"))
        self._host_sync = _HostSync(self._sync_counter, pos, self.clock() + timeout, by)
        if self.peer is not None and self.peer.separate:
            self._host_sync.peer_ready = True  # they're watching on their own: don't wait for them
        self.link.send({"t": "sync_prepare", "id": self._sync_counter, "pos": pos, **({"by": by} if by else {})})
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
        me_ready = self.p.is_ready() and abs(self.p.position() - sync.pos) < READY_TOLERANCE_MS
        timed_out = now >= sync.deadline
        if not ((sync.peer_ready and me_ready) or timed_out):
            return
        if timed_out and not sync.peer_ready and self.peer is not None:
            self.ui.problem("SW-503", name=self.peer.name)
        delay_ms = int(self.settings.get("sync.countdown_s")) * 1000
        self.link.send({"t": "sync_go", "id": sync.id, "delay": delay_ms})
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
            self.link.send({"t": "sync_ready", "id": client["id"]})

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
        peer = self.peer
        if peer is None or not self.film or not peer.film:
            return
        mine, theirs = self.film, peer.film
        if mine["size"] == theirs["size"]:
            state, text = "same", ""
        elif mine.get("dur") and theirs.get("dur") and abs(mine["dur"] - theirs["dur"]) <= MATCH_TOLERANCE_MS:
            state, text = "similar", f"{peer.name} has a different file of the same length. It should line up."
        elif not mine.get("dur") or not theirs.get("dur"):
            return  # wait until both lengths are known
        else:
            state = "different"
            text = (f"{peer.name}'s film is {fmt_time(theirs['dur'])} long ({theirs['name']}), yours is "
                    f"{fmt_time(mine['dur'])} ({mine['name']}).")
        if state == self.match_state:
            return
        self.match_state = state
        if state == "different":
            self.ui.problem("SW-501", detail=text)
        elif state == "similar":
            self.ui.activity(text, "info")


def _verb(action: str) -> str:
    return {"pause": "paused", "play": "pressed play", "seek": "jumped"}.get(action, action)
