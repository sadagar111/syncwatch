"""Version numbers: "0.3.1" for normal releases, "0.3.1-test.2" for test releases.

Order: 0.3.0 < 0.3.1-test.1 < 0.3.1-test.2 < 0.3.1 < 0.3.2. So someone on test
versions moves on to the finished 0.3.1 when it's published.
"""
from __future__ import annotations

import re
from functools import total_ordering
from typing import Optional, Tuple

_VERSION = re.compile(r"^(\d{1,4})\.(\d{1,4})\.(\d{1,4})(?:-test\.(\d{1,4}))?$")


@total_ordering
class Version:
    def __init__(self, text: str):
        match = _VERSION.match((text or "").strip().lstrip("v"))
        if not match:
            raise ValueError(f"not a SyncWatch version: {text!r}")
        self.major, self.minor, self.patch = (int(match.group(i)) for i in (1, 2, 3))
        self.test: Optional[int] = int(match.group(4)) if match.group(4) else None

    @property
    def is_test(self) -> bool:
        return self.test is not None

    def _key(self) -> Tuple[int, int, int, int]:
        # A finished version sorts after all its test versions.
        return (self.major, self.minor, self.patch, self.test if self.test is not None else 1_000_000)

    def __eq__(self, other) -> bool:
        return isinstance(other, Version) and self._key() == other._key()

    def __lt__(self, other: "Version") -> bool:
        return self._key() < other._key()

    def __hash__(self) -> int:
        return hash(self._key())

    def __str__(self) -> str:
        base = f"{self.major}.{self.minor}.{self.patch}"
        return f"{base}-test.{self.test}" if self.test is not None else base

    def __repr__(self) -> str:
        return f"Version('{self}')"


def parse(text: str) -> Optional[Version]:
    try:
        return Version(text)
    except ValueError:
        return None
