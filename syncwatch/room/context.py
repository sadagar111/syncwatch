"""Everything rooms need from the rest of the app, made once at startup."""
from __future__ import annotations

import os
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional

from ..net.relay import Relay
from ..net.relay_url import RELAY_URL
from .codes import CodeError, check_relay_address
from .identity import Identity, load_or_create
from .store import RoomStore


@dataclass
class Rooms:
    identity: Identity
    store: RoomStore
    relay_for: Callable[[str], Relay]
    default_relay: Callable[[], str]
    problems: List[str] = field(default_factory=list)


class RelayCache:
    """One Relay object per address (each keeps its own background poster)."""

    def __init__(self):
        self._relays: Dict[str, Relay] = {}
        self._lock = threading.Lock()

    def __call__(self, address: str) -> Relay:
        with self._lock:
            relay = self._relays.get(address)
            if relay is None:
                relay = self._relays[address] = Relay(address)
            return relay


def relay_address(settings) -> str:
    """The relay rooms use: SYNCWATCH_RELAY (tests) > Settings (advanced) > the built-in one."""
    for candidate in (os.environ.get("SYNCWATCH_RELAY", ""), settings.get("room.relay") or "", RELAY_URL):
        candidate = (candidate or "").strip()
        if candidate:
            try:
                return check_relay_address(candidate)
            except CodeError:
                continue
    return ""


def make_rooms(profile_dir: Optional[Path], settings) -> Rooms:
    """Load (or make) this PC's identity and the rooms file. Problems are SW codes to show."""
    problems: List[str] = []
    if profile_dir is None:  # the profile folder isn't writable: keys for this session only
        identity = Identity.generate()
        store = RoomStore(Path(os.devnull) / "rooms.json")
    else:
        identity, damaged = load_or_create(profile_dir / "identity.json")
        if damaged:
            problems.append("SW-414")
        store = RoomStore(profile_dir / "rooms.json")
        if store.problem:
            problems.append("SW-419")
    return Rooms(identity, store, RelayCache(), lambda: relay_address(settings), problems)
