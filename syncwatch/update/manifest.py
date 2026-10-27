"""The release manifest: what a release contains, signed by the publisher.

The signature covers the exact bytes of manifest.json. The manifest lists every
file with its size and SHA-256, so a valid signature vouches for all files.

Signature file format: one line of base64 (64-byte Ed25519 signature).
"""
from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from .version import Version

FORMAT = 1
APP = "SyncWatch"
MAX_MANIFEST_BYTES = 256 * 1024
MAX_NOTES_CHARS = 20_000
MAX_FILE_BYTES = 2 * 1024 ** 3
_HEX = set("0123456789abcdef")


class ManifestError(ValueError):
    """The manifest is unsigned, wrongly signed, or malformed (always SW-703)."""


@dataclass(frozen=True)
class FileRef:
    name: str
    size: int
    sha256: str
    tag: str = ""          # release (tag) that holds the file; "" = the manifest's own release


@dataclass(frozen=True)
class Manifest:
    version: Version
    channel: str           # "stable" or "test"
    released: str
    repo: str
    notes: str
    min_boot: int
    app_zip: FileRef
    runtime: Optional[FileRef]
    runtime_id: str
    full_zip: Optional[FileRef]
    raw: Dict[str, Any]


def public_key_text(key: Ed25519PublicKey) -> str:
    from cryptography.hazmat.primitives import serialization
    raw = key.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return base64.b64encode(raw).decode("ascii")


def load_public_key(text: str) -> Ed25519PublicKey:
    raw = base64.b64decode(text.strip(), validate=True)
    if len(raw) != 32:
        raise ValueError("an Ed25519 public key is 32 bytes")
    return Ed25519PublicKey.from_public_bytes(raw)


def sign(manifest_bytes: bytes, private_key: Ed25519PrivateKey) -> bytes:
    return base64.b64encode(private_key.sign(manifest_bytes)) + b"\n"


def verify_signature(manifest_bytes: bytes, signature_text: bytes, trusted_keys: Iterable[str]) -> str:
    """Returns the trusted key that signed it, or raises ManifestError."""
    if len(manifest_bytes) > MAX_MANIFEST_BYTES:
        raise ManifestError("manifest too large")
    try:
        signature = base64.b64decode(signature_text.strip(), validate=True)
    except (ValueError, TypeError) as exc:
        raise ManifestError(f"signature file is not valid base64: {exc}") from exc
    if len(signature) != 64:
        raise ManifestError("signature has the wrong length")
    keys = [k for k in trusted_keys if k]
    if not keys:
        raise ManifestError("no trusted keys configured")
    for key_text in keys:
        try:
            load_public_key(key_text).verify(signature, manifest_bytes)
            return key_text
        except InvalidSignature:
            continue
        except ValueError:
            continue  # a malformed key in the list must not stop the others
    raise ManifestError("signature does not match any trusted key")


def _file_ref(raw: Any, field: str, required: bool) -> Optional[FileRef]:
    if raw is None:
        if required:
            raise ManifestError(f"missing {field}")
        return None
    if not isinstance(raw, dict):
        raise ManifestError(f"{field} is not an object")
    name, size, digest, tag = raw.get("name"), raw.get("size"), raw.get("sha256"), raw.get("tag", "")
    if not isinstance(name, str) or not name or "/" in name or "\\" in name or name.startswith("."):
        raise ManifestError(f"{field}.name is invalid")
    if isinstance(size, bool) or not isinstance(size, int) or not 0 < size <= MAX_FILE_BYTES:
        raise ManifestError(f"{field}.size is invalid")
    if not isinstance(digest, str) or len(digest) != 64 or not set(digest) <= _HEX:
        raise ManifestError(f"{field}.sha256 is invalid")
    if not isinstance(tag, str) or len(tag) > 64 or (tag and not tag.startswith("v")):
        raise ManifestError(f"{field}.tag is invalid")
    return FileRef(name, size, digest, tag)


def parse(manifest_bytes: bytes, expected_repo: str) -> Manifest:
    """Parse an already signature-checked manifest. Raises ManifestError if anything is off."""
    try:
        raw = json.loads(manifest_bytes.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ManifestError(f"manifest is not valid JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise ManifestError("manifest is not an object")
    if raw.get("format") != FORMAT or raw.get("app") != APP:
        raise ManifestError("not a SyncWatch manifest of a supported format")
    try:
        version = Version(str(raw.get("version", "")))
    except ValueError as exc:
        raise ManifestError(str(exc)) from exc
    channel = raw.get("channel")
    if channel not in ("stable", "test") or (channel == "test") != version.is_test:
        raise ManifestError("channel doesn't match the version")
    repo = raw.get("repo")
    if not isinstance(repo, str) or repo.lower() != expected_repo.lower():
        raise ManifestError(f"manifest is for another repository ({repo!r})")
    notes = raw.get("notes", "")
    if not isinstance(notes, str):
        raise ManifestError("notes are not text")
    min_boot = raw.get("min_boot", 1)
    if isinstance(min_boot, bool) or not isinstance(min_boot, int) or min_boot < 1:
        raise ManifestError("min_boot is invalid")
    runtime_id = raw.get("runtime_id", "")
    if not isinstance(runtime_id, str) or not runtime_id or len(runtime_id) > 40 \
            or not all(c.isalnum() or c in "-_." for c in runtime_id):
        raise ManifestError("runtime_id is invalid")
    released = raw.get("released", "")
    return Manifest(
        version=version, channel=channel, released=released if isinstance(released, str) else "",
        repo=repo, notes=notes[:MAX_NOTES_CHARS], min_boot=min_boot,
        app_zip=_file_ref(raw.get("app_zip"), "app_zip", True),
        runtime=_file_ref(raw.get("runtime"), "runtime", False),
        runtime_id=runtime_id,
        full_zip=_file_ref(raw.get("full_zip"), "full_zip", False),
        raw=raw)


def build(version: str, repo: str, notes: str, runtime_id: str, app_zip: FileRef,
          runtime: Optional[FileRef], full_zip: Optional[FileRef], released: str, min_boot: int = 1) -> bytes:
    """Manifest bytes for publishing (used by tools/release.py)."""
    parsed = Version(version)

    def ref(r: Optional[FileRef]) -> Optional[Dict[str, Any]]:
        if r is None:
            return None
        data = {"name": r.name, "size": r.size, "sha256": r.sha256}
        if r.tag:
            data["tag"] = r.tag
        return data

    data = {"format": FORMAT, "app": APP, "version": str(parsed), "channel": "test" if parsed.is_test else "stable",
            "released": released, "repo": repo, "notes": notes, "min_boot": min_boot, "runtime_id": runtime_id,
            "app_zip": ref(app_zip), "runtime": ref(runtime), "full_zip": ref(full_zip)}
    return (json.dumps(data, indent=1, ensure_ascii=False) + "\n").encode("utf-8")


def sha256_file(path, chunk: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            block = handle.read(chunk)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()
