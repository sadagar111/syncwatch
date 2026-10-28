"""Who you are: this PC's own keys, made once on first start.

Two key pairs:

* **signing** (Ed25519): every message you send carries your signature, so
  nobody can send messages in your name, not even someone in the same room;
* **encryption** (X25519): others can send things only you can read, like the
  room key when the host lets you in.

The public halves together give your *fingerprint* (SHA-256). Shown to people
as a short ID like ``K7QF-2M9X`` next to your name: if someone takes a
friend's name, the ID gives them away (and the app compares the full
fingerprint anyway).

Also here: your *mailbox* secret. It decides where friends' apps leave
messages for you (invitations to rooms you can join). Only friends learn it.

The private keys are stored unencrypted in your profile folder
(``identity.json``), like an SSH key without a passphrase. Anyone who can read
your user profile could take them; nobody else can.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Tuple

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey

from .codes import b32encode, group

log = logging.getLogger("syncwatch.identity")

FORMAT = 1
KEY_BYTES = 32
_RAW = serialization.Encoding.Raw


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _unb64(text: str, length: int) -> bytes:
    data = base64.b64decode(text, validate=True)
    if len(data) != length:
        raise ValueError("wrong key length")
    return data


@dataclass(frozen=True)
class PublicIdentity:
    """Someone's public keys: enough to check their signatures and encrypt to them."""
    sign_pub: bytes
    dh_pub: bytes

    def __post_init__(self) -> None:
        if len(self.sign_pub) != KEY_BYTES or len(self.dh_pub) != KEY_BYTES:
            raise ValueError("public keys must be 32 bytes")

    @property
    def fingerprint(self) -> bytes:
        return hashlib.sha256(b"SyncWatch identity v1\x00" + self.sign_pub + self.dh_pub).digest()

    @property
    def key_id(self) -> bytes:
        """8 bytes that name this identity in messages and in room codes."""
        return self.fingerprint[:8]

    @property
    def short_id(self) -> str:
        """What people see, e.g. ``K7QF-2M9X`` (40 bits of the fingerprint)."""
        return group(b32encode(self.fingerprint[:5]))

    def verify(self, signature: bytes, data: bytes) -> bool:
        try:
            Ed25519PublicKey.from_public_bytes(self.sign_pub).verify(signature, data)
            return True
        except Exception:  # InvalidSignature, or a malformed key
            return False

    def to_json(self) -> dict:
        return {"sign": _b64(self.sign_pub), "dh": _b64(self.dh_pub)}

    @classmethod
    def from_json(cls, data: dict) -> "PublicIdentity":
        return cls(_unb64(data["sign"], KEY_BYTES), _unb64(data["dh"], KEY_BYTES))


class Identity:
    """My own keys (private halves included)."""

    def __init__(self, sign_seed: bytes, dh_secret: bytes, mailbox_secret: bytes, created: float = 0.0):
        if len(sign_seed) != KEY_BYTES or len(dh_secret) != KEY_BYTES or len(mailbox_secret) != 16:
            raise ValueError("bad identity key sizes")
        self._sign = Ed25519PrivateKey.from_private_bytes(sign_seed)
        self._dh = X25519PrivateKey.from_private_bytes(dh_secret)
        self.mailbox_secret = mailbox_secret
        self.created = created
        self.public = PublicIdentity(self._sign.public_key().public_bytes(_RAW, serialization.PublicFormat.Raw),
                                     self._dh.public_key().public_bytes(_RAW, serialization.PublicFormat.Raw))

    @classmethod
    def generate(cls) -> "Identity":
        return cls(os.urandom(KEY_BYTES), os.urandom(KEY_BYTES), os.urandom(16), time.time())

    def sign(self, data: bytes) -> bytes:
        return self._sign.sign(data)

    def exchange(self, their_dh_pub: bytes) -> bytes:
        return self._dh.exchange(X25519PublicKey.from_public_bytes(their_dh_pub))

    def _private_json(self) -> dict:
        sign_seed = self._sign.private_bytes(_RAW, serialization.PrivateFormat.Raw, serialization.NoEncryption())
        dh_secret = self._dh.private_bytes(_RAW, serialization.PrivateFormat.Raw, serialization.NoEncryption())
        return {"format": FORMAT, "sign": _b64(sign_seed), "dh": _b64(dh_secret),
                "mailbox": _b64(self.mailbox_secret), "created": self.created,
                "_note": "Your SyncWatch identity. Keep it private: whoever has this file can act as you."}

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(self._private_json(), indent=1), encoding="utf-8")
        os.replace(tmp, path)

    def __repr__(self) -> str:  # never print private keys by accident
        return f"Identity({self.public.short_id})"


def load_or_create(path: Path) -> Tuple[Identity, Optional[str]]:
    """Load this PC's identity, or make one. Returns (identity, problem).

    ``problem`` is set when a damaged file was replaced by a new identity: then
    friends see a new person and have to add you again. The damaged file is
    kept next to it (``identity.json.damaged-<time>``) so nothing is lost.
    """
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if data.get("format") != FORMAT:
                raise ValueError(f"unknown format {data.get('format')!r}")
            identity = Identity(_unb64(data["sign"], KEY_BYTES), _unb64(data["dh"], KEY_BYTES),
                                _unb64(data["mailbox"], 16), float(data.get("created", 0.0)))
            return identity, None
        except (OSError, ValueError, KeyError, TypeError) as exc:
            damaged = path.with_name(f"{path.name}.damaged-{int(time.time())}")
            try:
                os.replace(path, damaged)
            except OSError:
                pass
            problem = f"{type(exc).__name__}: {exc}"
            log.error("identity file damaged (%s); made a new identity, old file kept as %s", problem, damaged.name)
            identity = Identity.generate()
            identity.save(path)
            return identity, problem
    identity = Identity.generate()
    identity.save(path)
    log.info("made a new identity %s", identity.public.short_id)
    return identity, None
