"""Friends: the friend list, friend requests, and invitations to rooms.

(docs/ROOMS.md, "Friends".) Each person's app listens on its *mailbox*, a relay
channel only friends know. When you host, your app leaves a sealed, signed
"I'm hosting, here's the code" in each friend's mailbox, repeats it every few
minutes, and says when the room closes. When you start SyncWatch, your app asks
your friends' mailboxes "anyone hosting?", so rooms that are already open show
up too. Messages from anyone who isn't a friend are dropped unread.

Becoming friends happens inside a room (the other person has to accept), so
you only ever add people you've actually watched with.
"""
from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Tuple

from ..net import protocol
from . import crypto
from .codes import CodeError, RoomCode, parse
from .identity import Identity, PublicIdentity
from .store import Friend, RoomStore

log = logging.getLogger("syncwatch.friends")

ANNOUNCE_EVERY_S = 5 * 60
ASK_EVERY_S = 10 * 60
ROOM_EXPIRES_S = 12 * 60
MAX_CLOCK_SKEW_S = 10 * 60
REQUEST_EXPIRES_S = 10 * 60
MAIL_KINDS = {"hosting", "hosting_end", "who_hosts", "unfriend", "friend_ok"}


@dataclass
class FriendRoom:
    code: RoomCode
    room: str
    seen: float


@dataclass
class Request:
    key: PublicIdentity
    name: str
    mailbox: str
    at: float

    @property
    def id(self) -> bytes:
        return self.key.key_id


class Friends:
    def __init__(self, identity: Identity, store: RoomStore, settings, listener,
                 sink: Callable[[Dict], None], relay_for: Callable[[str], object],
                 default_relay: Callable[[], str], clock: Callable[[], float] = time.monotonic,
                 wall: Callable[[], float] = time.time):
        self.me = identity
        self.store = store
        self.settings = settings
        self.listener = listener
        self.sink = sink
        self.relay_for = relay_for
        self.default_relay = default_relay
        self.clock = clock
        self.wall = wall
        self.mailbox_bytes = crypto.mailbox_channel(identity.mailbox_secret)
        self.mailbox_id = self.mailbox_bytes.hex()
        self.rooms: Dict[bytes, FriendRoom] = {}
        self.incoming: Dict[bytes, Request] = {}
        self.outgoing: Dict[bytes, Tuple[PublicIdentity, float]] = {}   # requests I sent
        self.hosting: Optional[Tuple[RoomCode, str]] = None
        self.guard = crypto.ReplayGuard()
        self.session_id = os.urandom(8).hex()
        self._seq = 0
        self._gen = 0
        self._channel = None
        self._relay = None
        self._asked = False
        self._next_announce = 0.0
        self._next_ask = 0.0

    # ================================================================ mailbox
    def start(self) -> None:
        address = self.default_relay()
        if not address or self._channel is not None:
            return
        self._relay = self.relay_for(address)
        self._gen += 1
        gen = self._gen
        self._channel = self._relay.channel(
            self.mailbox_id,
            on_message=lambda data: self.sink({"t": "_mail", "gen": gen, "data": data}),
            on_state=lambda state, error: self.sink({"t": "_mail_state", "gen": gen, "state": state}),
            name="mailbox").start()

    def stop(self) -> None:
        self._gen += 1
        if self._channel is not None:
            self._channel.close()
        self._channel = None

    def handle_event(self, event: Dict) -> None:
        if event.get("gen") != self._gen:
            return
        if event["t"] == "_mail_state":
            if event["state"] == "connected" and not self._asked:
                self._asked = True
                self.ask_who_hosts()
            return
        self._on_mail(event["data"])

    def _my_name(self) -> str:
        return (self.settings.get("profile.name") or "Someone").strip()[:32] or "Someone"

    def _friend_by_sign(self, sign_pub: bytes) -> Optional[Friend]:
        for friend in self.store.friends.values():
            if friend.key.sign_pub == sign_pub:
                return friend
        return None

    def _on_mail(self, data: bytes) -> None:
        if not crypto.is_for_me(data, self.me):
            return
        try:
            opened = crypto.open_direct(data, self.mailbox_bytes, self.me)
            body = protocol.validate(protocol.DIRECT, opened.body)
        except (crypto.BadEnvelope, protocol.BadMessage) as exc:
            log.debug("dropped mailbox message: %s", exc)
            return
        if body is None or body["t"] not in MAIL_KINDS:
            return
        friend = self._friend_by_sign(opened.sender)
        if friend is None:
            # The only thing a non-friend may send: accepting a request I made (after we left the room).
            if body["t"] == "friend_ok" and "dh" in body:
                try:
                    key = PublicIdentity(opened.sender, body["dh"])
                except ValueError:
                    return
                self._accepted(key, body)
            return
        if not self.guard.accept(opened.sender, body["s"], body["n"]):
            return
        if body["t"] != "friend_ok" and abs(self.wall() - body["ts"]) > MAX_CLOCK_SKEW_S:
            return  # too old (or a PC clock far off): don't act on it
        kind = body["t"]
        key_id = friend.key.key_id
        if kind == "hosting":
            try:
                code = parse(body["code"])
            except CodeError:
                return
            if code.host_commit != key_id:
                return  # a friend can only invite to their own room
            new = key_id not in self.rooms
            self.rooms[key_id] = FriendRoom(code, body["room"], self.clock())
            if new and self.settings.get("friends.show_rooms"):
                self.listener.friends_changed("hosting", name=friend.name)
            else:
                self.listener.friends_changed("rooms")
        elif kind == "hosting_end":
            if self.rooms.pop(key_id, None) is not None:
                self.listener.friends_changed("rooms")
        elif kind == "who_hosts":
            if self.hosting is not None and self.settings.get("friends.announce"):
                self._post(friend, "hosting", code=self.hosting[0].text(), room=self.hosting[1], ts=self.wall())
        elif kind == "unfriend":
            self.store.remove_friend(key_id)
            self.rooms.pop(key_id, None)
            self.listener.friends_changed("removed", name=friend.name)
        elif kind == "friend_ok":
            self._accepted(friend.key, body)

    def _body(self, kind: str, **fields) -> Dict:
        self._seq += 1
        return {"t": kind, "s": self.session_id, "n": self._seq, **fields}

    def _post(self, friend: Friend, kind: str, **fields) -> bool:
        return self._post_to(friend.key, friend.mailbox, kind, **fields)

    def _post_to(self, key: PublicIdentity, mailbox: str, kind: str, **fields) -> bool:
        relay = self._relay or (self.relay_for(self.default_relay()) if self.default_relay() else None)
        if relay is None:
            return False
        data = crypto.pack_direct(bytes.fromhex(mailbox), key, self.me, self._body(kind, **fields))
        return relay.post_later(mailbox, data)

    # ================================================================ hosting
    def hosting_started(self, code: RoomCode, room: str) -> None:
        self.hosting = (code, room)
        self._next_announce = self.clock() + ANNOUNCE_EVERY_S
        self._announce()

    def hosting_stopped(self, wait: bool = False) -> None:
        """``wait``: post right now (SyncWatch is closing, background posts wouldn't get out)."""
        if self.hosting is None:
            return
        self.hosting = None
        if not self.settings.get("friends.announce"):
            return
        deadline = time.monotonic() + 5.0
        for friend in list(self.store.friends.values()):
            if wait:
                if time.monotonic() > deadline:
                    break  # friends' lists forget the room by themselves after a few minutes
                self._post_now(friend, "hosting_end", ts=self.wall())
            else:
                self._post(friend, "hosting_end", ts=self.wall())

    def _post_now(self, friend: Friend, kind: str, **fields) -> None:
        relay = self._relay or (self.relay_for(self.default_relay()) if self.default_relay() else None)
        if relay is None:
            return
        data = crypto.pack_direct(bytes.fromhex(friend.mailbox), friend.key, self.me, self._body(kind, **fields))
        try:
            relay.post(friend.mailbox, data, timeout=2.0)
        except Exception as exc:  # best effort while closing
            log.info("couldn't tell %s the room closed: %s", friend.name, exc)

    def _announce(self) -> None:
        if self.hosting is None or not self.settings.get("friends.announce"):
            return
        code, room = self.hosting
        for friend in list(self.store.friends.values()):
            self._post(friend, "hosting", code=code.text(), room=room, ts=self.wall())

    def ask_who_hosts(self) -> None:
        self._next_ask = self.clock() + ASK_EVERY_S
        if not self.settings.get("friends.show_rooms"):
            return
        for friend in list(self.store.friends.values()):
            self._post(friend, "who_hosts", ts=self.wall())

    def tick(self, session=None) -> None:
        now = self.clock()
        if self.hosting is not None and now >= self._next_announce:
            self._next_announce = now + ANNOUNCE_EVERY_S
            self._announce()
        if self._asked and now >= self._next_ask:
            self.ask_who_hosts()
        expired = [k for k, room in self.rooms.items() if now - room.seen > ROOM_EXPIRES_S]
        for key_id in expired:
            del self.rooms[key_id]
        old = [k for k, request in self.incoming.items() if now - request.at > REQUEST_EXPIRES_S]
        for key_id in old:
            del self.incoming[key_id]
        for key_id in [k for k, (_, at) in self.outgoing.items() if now - at > REQUEST_EXPIRES_S]:
            del self.outgoing[key_id]   # a very late "yes" doesn't count any more
        if expired or old:
            self.listener.friends_changed("rooms")

    def room_of(self, key_id: bytes) -> Optional[FriendRoom]:
        if not self.settings.get("friends.show_rooms"):
            return None
        return self.rooms.get(key_id)

    def friend_list(self) -> List[Friend]:
        """Friends hosting first, then by name."""
        return sorted(self.store.friends.values(),
                      key=lambda f: (self.room_of(f.key.key_id) is None, f.name.lower()))

    def is_friend(self, key: PublicIdentity) -> bool:
        return self.store.is_friend(key)

    # ================================================================ requests (inside a room)
    def request(self, session, member) -> bool:
        if self.store.is_friend(member.key):
            return False
        if member.id in self.incoming:  # they asked me already: that's a yes from both sides
            return self.accept(session, member.id)
        ok = session.send_to(member.id, "friend_req", name=self._my_name(), mailbox=self.mailbox_id)
        if ok:
            self.outgoing[member.id] = (member.key, self.clock())
            self.listener.friends_changed("requested", name=member.name)
        return ok

    def handle_room(self, session, sender: PublicIdentity, member, body: Dict) -> None:
        kind = body["t"]
        if kind == "friend_req":
            if self.store.is_blocked(sender):
                return
            pending = self.outgoing.get(sender.key_id)
            if self.store.is_friend(sender) or (pending is not None and pending[0] == sender):
                # Already friends (they lost their list?) or we both asked: confirm right away.
                self._add(sender, body["name"], body["mailbox"])
                session.send_to(sender.key_id, "friend_ok", name=self._my_name(), mailbox=self.mailbox_id)
                return
            new = sender.key_id not in self.incoming
            self.incoming[sender.key_id] = Request(sender, body["name"], body["mailbox"], self.clock())
            if new:
                self.listener.friends_changed("request", name=body["name"])
        elif kind == "friend_ok":
            self._accepted(sender, body)
        elif kind == "friend_no":
            if self.outgoing.pop(sender.key_id, None) is not None:
                self.listener.friends_changed("declined", name=member.name if member else "Someone")

    def _accepted(self, key: PublicIdentity, body: Dict) -> None:
        """They said yes to my request (in the room, or later through my mailbox)."""
        pending = self.outgoing.get(key.key_id)
        if (pending is None or pending[0] != key) and not self.store.is_friend(key):
            return
        self.outgoing.pop(key.key_id, None)
        new = not self.store.is_friend(key)
        self._add(key, body["name"], body["mailbox"])
        if new:
            self.listener.friends_changed("added", name=body["name"])

    def _add(self, key: PublicIdentity, name: str, mailbox: str) -> None:
        self.store.add_friend(key, name, mailbox)
        if self.hosting is not None and self.settings.get("friends.announce"):
            friend = self.store.friends.get(key.key_id)
            if friend is not None:
                code, room = self.hosting
                self._post(friend, "hosting", code=code.text(), room=room, ts=self.wall())

    def accept(self, session, key_id: bytes) -> bool:
        request = self.incoming.pop(key_id, None)
        if request is None:
            return False
        self._add(request.key, request.name, request.mailbox)
        sent = session is not None and session.send_to(key_id, "friend_ok", name=self._my_name(),
                                                         mailbox=self.mailbox_id)
        if not sent:  # they left the room meanwhile: tell them through their mailbox
            self._post_to(request.key, request.mailbox, "friend_ok", name=self._my_name(), mailbox=self.mailbox_id,
                          dh=protocol.b64(self.me.public.dh_pub))
        self.listener.friends_changed("added", name=request.name)
        return True

    def decline(self, session, key_id: bytes) -> bool:
        request = self.incoming.pop(key_id, None)
        if request is None:
            return False
        if session is not None:
            session.send_to(key_id, "friend_no")
        self.listener.friends_changed("rooms")
        return True

    def remove(self, key_id: bytes) -> Optional[Friend]:
        friend = self.store.remove_friend(key_id)
        if friend is None:
            return None
        self.rooms.pop(key_id, None)
        self._post(friend, "unfriend", ts=self.wall())
        self.listener.friends_changed("removed", name=friend.name)
        return friend
