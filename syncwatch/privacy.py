"""What must never end up in a log, or in a report someone pastes into a chat.

* Keys and passwords ("token=…", "api_key: …"): replaced by ***.
* Addresses keep only their host. A YouTube stream address holds the IP address it was given
  to and its signatures, a Real-Debrid link works as its own password, and the local torrent
  server's address has a secret token in it. "https://rr4---sn-x.googlevideo.com/…" is enough
  to see what went wrong.
"""
from __future__ import annotations

import re

_SECRET = re.compile(r"(?i)\b(token|secret|password|passphrase|api[_-]?key|private[_-]?key)"
                     r"(\s*[=:]\s*|\"\s*:\s*\")([^\s\",]{4,})")
# scheme, host (maybe with user:password@ and :port), then the rest up to a space or a quote
_ADDRESS = re.compile(r"(?i)\b((?:https?|wss?|rtmps?|ftps?)://)([^\s/?#'\"<>]*)([^\s'\"<>]*)")


def redact(text: str) -> str:
    return _SECRET.sub(lambda m: f"{m.group(1)}{m.group(2)}***", text)


def _host_only(match: re.Match) -> str:
    host = match.group(2).rsplit("@", 1)[-1]          # never a user:password@
    rest = match.group(3)
    return match.group(1) + host + (rest if rest in ("", "/") else "/…")


def hide_addresses(text: str) -> str:
    """Every address in ``text`` cut down to its host: 'https://x.example/a?b' -> 'https://x.example/…'."""
    return _ADDRESS.sub(_host_only, text)


def clean(text: str) -> str:
    """Both: what logs and reports go through."""
    return hide_addresses(redact(text))
