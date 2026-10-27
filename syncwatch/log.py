"""Logging to a file in your profile, plus the "Copy report" text.

Log files rotate (1 MB each, a few kept), so they never grow without limit.
The last few hundred lines are also kept in memory so "Copy report" can
include them without reading files.
"""
from __future__ import annotations

import collections
import logging
import logging.handlers
import re
import threading
from pathlib import Path
from typing import Iterable, Optional

from . import errors

LOG_FORMAT = "%(asctime)s %(levelname)-7s [%(threadName)s] %(name)s: %(message)s"
_ring_lock = threading.Lock()

# Things that must never end up in a report someone pastes into a chat.
# (Step 1 has no secrets yet; this already covers tokens/keys added later.)
_SECRET = re.compile(r"(?i)\b(token|secret|password|passphrase|api[_-]?key|private[_-]?key)"
                     r"(\s*[=:]\s*|\"\s*:\s*\")([^\s\",]{4,})")


def redact(text: str) -> str:
    return _SECRET.sub(lambda m: f"{m.group(1)}{m.group(2)}***", text)


class RingHandler(logging.Handler):
    """Keeps the most recent log lines in memory."""

    def __init__(self, capacity: int = 400):
        super().__init__()
        self.lines: collections.deque = collections.deque(maxlen=capacity)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            line = self.format(record)
        except Exception:  # pragma: no cover
            return
        with _ring_lock:
            self.lines.append(line)

    def tail(self, count: int = 200) -> list:
        with _ring_lock:
            return list(self.lines)[-count:]


ring = RingHandler()
_file_handler: Optional[logging.Handler] = None


def setup(logs_dir: Optional[Path], debug: bool = False, keep_files: int = 5) -> Optional[Path]:
    """Configure logging. Returns the log file path, or None if it can't be written."""
    global _file_handler
    root = logging.getLogger("syncwatch")
    root.setLevel(logging.DEBUG if debug else logging.INFO)
    root.propagate = False
    formatter = logging.Formatter(LOG_FORMAT)

    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()

    ring.setFormatter(formatter)
    root.addHandler(ring)

    console = logging.StreamHandler()
    console.setLevel(logging.WARNING)
    console.setFormatter(formatter)
    root.addHandler(console)

    log_path = None
    if logs_dir is not None:
        try:
            logs_dir.mkdir(parents=True, exist_ok=True)
            log_path = logs_dir / "syncwatch.log"
            _file_handler = logging.handlers.RotatingFileHandler(
                log_path, maxBytes=1_000_000, backupCount=max(1, keep_files), encoding="utf-8")
            _file_handler.setFormatter(formatter)
            root.addHandler(_file_handler)
        except OSError:
            log_path = None
    return log_path


def set_debug(debug: bool) -> None:
    logging.getLogger("syncwatch").setLevel(logging.DEBUG if debug else logging.INFO)


def build_report(sections: Iterable[tuple] = (), log_lines: int = 200) -> str:
    """Text for the "Copy report" button: versions, check results, recent log."""
    parts = ["=== SyncWatch report ===", errors.system_summary(), ""]
    for title, body in sections:
        parts.append(f"--- {title} ---")
        parts.append(str(body).rstrip())
        parts.append("")
    parts.append(f"--- Last {log_lines} log lines ---")
    parts.extend(ring.tail(log_lines))
    return redact("\n".join(parts))
