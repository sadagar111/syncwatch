"""Per-film memory: where you stopped, which subtitles/audio you picked, subtitle delay.

A film is identified by its file name plus size, so renaming a film or
replacing it with a different release starts fresh. The file only grows to
``MAX_FILMS`` entries; the least recently used ones are dropped.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

log = logging.getLogger("syncwatch.films")

MAX_FILMS = 500
_ALLOWED_FIELDS = {"pos_ms": int, "subs": str, "sub_delay_ms": int, "sub_stretch": float,
                   "audio_track": int, "used": float}


def film_key(name: str, size: int) -> str:
    return f"{name}|{size}"


class FilmMemory:
    def __init__(self, path: Optional[Path]):
        self.path = path
        self._films: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.Lock()
        self._dirty = False

    def load(self, old_positions: Optional[Path] = None) -> list:
        """Read the file. Returns problem codes to report."""
        problems = []
        if self.path is not None and self.path.exists():
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                if not isinstance(raw, dict):
                    raise ValueError("not an object")
                for key, entry in raw.items():
                    if isinstance(key, str) and isinstance(entry, dict):
                        self._films[key] = self._clean(entry)
            except (OSError, ValueError) as exc:
                log.warning("film memory damaged (%s); starting fresh", exc)
                problems.append("SW-107")
        if old_positions is not None and old_positions.exists() and not self._films:
            self._import_old(old_positions)
        return problems

    def _import_old(self, old: Path) -> None:
        """Version 0.1 stored only positions, next to the app. Bring them over once."""
        try:
            raw = json.loads(old.read_text(encoding="utf-8"))
            for key, pos in raw.items():
                if isinstance(key, str) and isinstance(pos, (int, float)) and pos >= 0:
                    self._films[key] = {"pos_ms": int(pos), "used": time.time()}
            self._dirty = True
            log.info("imported %d positions from version 0.1", len(self._films))
        except (OSError, ValueError, AttributeError):
            pass

    @staticmethod
    def _clean(entry: Dict[str, Any]) -> Dict[str, Any]:
        clean = {}
        for field, kind in _ALLOWED_FIELDS.items():
            value = entry.get(field)
            if isinstance(value, bool):
                continue
            if kind is float and isinstance(value, (int, float)):
                clean[field] = float(value)
            elif kind is int and isinstance(value, (int, float)):
                clean[field] = int(value)
            elif kind is str and isinstance(value, str):
                clean[field] = value[:1000]
        return clean

    def get(self, key: str) -> Dict[str, Any]:
        with self._lock:
            return dict(self._films.get(key, {}))

    def update(self, key: str, **fields: Any) -> None:
        with self._lock:
            entry = self._films.setdefault(key, {})
            entry.update(self._clean(fields))
            entry["used"] = time.time()
            self._dirty = True
            if len(self._films) > MAX_FILMS:
                oldest = sorted(self._films, key=lambda k: self._films[k].get("used", 0))
                for stale in oldest[: len(self._films) - MAX_FILMS]:
                    del self._films[stale]

    def save_if_dirty(self) -> None:
        if not self._dirty or self.path is None:
            return
        with self._lock:
            data = json.dumps(self._films, indent=1, ensure_ascii=False)
            self._dirty = False
        try:
            tmp = self.path.with_name(self.path.name + ".tmp")
            tmp.write_text(data, encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError as exc:
            log.warning("could not save film memory: %s", exc)
