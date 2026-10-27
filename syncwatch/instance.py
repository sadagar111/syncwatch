"""Makes sure only one SyncWatch window runs per profile (SW-108).

Uses an operating-system file lock, which the OS releases automatically if
SyncWatch crashes, so a crash can never leave you "locked out".
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Optional


class InstanceLock:
    def __init__(self, path: Path):
        self.path = path
        self._handle = None

    def acquire(self) -> bool:
        """Return True if we got the lock, False if another window holds it."""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            handle = open(self.path, "a+b")
        except OSError:
            return True  # can't even create the file: don't block startup over it
        try:
            if sys.platform == "win32":
                import msvcrt
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            return False
        try:
            handle.seek(0)
            handle.truncate()
            handle.write(str(os.getpid()).encode())
            handle.flush()
        except OSError:
            pass
        self._handle = handle
        return True

    def release(self) -> None:
        handle, self._handle = self._handle, None
        if handle is None:
            return
        try:
            if sys.platform == "win32":
                import msvcrt
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        handle.close()


_held: Optional[InstanceLock] = None


def hold(path: Path) -> bool:
    """Acquire and keep the lock for the lifetime of the process."""
    global _held
    lock = InstanceLock(path)
    if lock.acquire():
        _held = lock
        return True
    return False
