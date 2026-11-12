"""Logging to a file in your profile, plus the "Copy report" text.

Log files rotate (1 MB each, a few kept), so they never grow without limit.
The last few hundred lines are also kept in memory so "Copy report" can
include them without reading files.

Every line goes through privacy.clean(): no keys, and addresses keep only their host (mpv's
own messages carry the full address it couldn't open).

On Windows a log file can't be renamed while another program has it open. Python's own
rotating handler then drops every line from that moment on, without a word: on Alex's PC the
log stopped on 28 Oct 2026. Here the file just keeps growing until a new one can be started,
and status() tells the startup checks (SW-113).
"""
from __future__ import annotations

import collections
import logging
import logging.handlers
import os
import threading
import time
from pathlib import Path
from typing import Iterable, Optional, Tuple

from . import errors
from .privacy import clean

LOG_FORMAT = "%(asctime)s %(levelname)-7s [%(threadName)s] %(name)s: %(message)s"
_ring_lock = threading.Lock()

ROTATE_RETRY_S = 300       # after a failed rename, try starting a new file again this much later


class SafeFormatter(logging.Formatter):
    """The usual format, then privacy.clean(): no keys, addresses only up to the host."""

    def format(self, record: logging.LogRecord) -> str:
        return clean(super().format(record))


class SafeRotatingFileHandler(logging.handlers.RotatingFileHandler):
    """RotatingFileHandler that never goes silent (see the module text).

    * Starting a new file fails (the old one can't be renamed): keep writing to the old one and
      try again after ROTATE_RETRY_S.
    * The file can't be opened at all: write to syncwatch-<process>.log next to it instead.
    * Anything else that goes wrong while writing is counted, not printed (a window app has no
      console), and status() reports it.
    """

    def __init__(self, filename, **kwargs):
        self.rotation_problem = ""     # why the last new file couldn't be started ("" = fine)
        self.write_problem = ""        # the last error while writing a line
        self.failed_lines = 0
        self.fallback = False          # writing to syncwatch-<process>.log instead
        self._retry_at = 0.0
        try:
            super().__init__(filename, **kwargs)
        except OSError as exc:
            other = Path(filename).with_name(f"{Path(filename).stem}-{os.getpid()}{Path(filename).suffix}")
            super().__init__(other, **kwargs)
            self.fallback = True
            self.write_problem = errors.describe_exception(exc)

    def shouldRollover(self, record) -> bool:  # noqa: N802 - logging's name
        if self._retry_at and time.monotonic() < self._retry_at:
            return False
        return super().shouldRollover(record)

    def doRollover(self) -> None:  # noqa: N802 - logging's name
        try:
            super().doRollover()
            self.rotation_problem, self._retry_at = "", 0.0
        except OSError as exc:
            self.rotation_problem = errors.describe_exception(exc)
            self._retry_at = time.monotonic() + ROTATE_RETRY_S
            if self.stream is None:            # (the base class closed it before renaming)
                self.stream = self._open()

    def handleError(self, record) -> None:  # noqa: N802 - logging's name
        self.failed_lines += 1
        import sys  # noqa: PLC0415
        exc = sys.exc_info()[1]
        self.write_problem = errors.describe_exception(exc) if exc else "unknown"


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
    formatter = SafeFormatter(LOG_FORMAT)

    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()
    _file_handler = None

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
            _file_handler = SafeRotatingFileHandler(
                log_path, maxBytes=1_000_000, backupCount=max(1, keep_files), encoding="utf-8")
            _file_handler.setFormatter(formatter)
            root.addHandler(_file_handler)
            log_path = Path(_file_handler.baseFilename)
        except OSError:
            _file_handler = None
            log_path = None
    return log_path


def status() -> Tuple[str, str, str]:
    """For the startup checks: (state, where, why). state: "ok", "growing" (a new file couldn't be
    started), "elsewhere" (writing to syncwatch-<process>.log), "lines lost", or "none" (no file)."""
    handler = _file_handler
    if handler is None:
        return "none", "", ""
    where = handler.baseFilename
    if handler.fallback:
        return "elsewhere", where, handler.write_problem
    if handler.failed_lines:
        return "lines lost", where, f"{handler.failed_lines} lines: {handler.write_problem}"
    if handler.rotation_problem:
        return "growing", where, handler.rotation_problem
    return "ok", where, ""


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
    return clean("\n".join(parts))
