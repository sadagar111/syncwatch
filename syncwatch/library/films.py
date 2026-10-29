"""The library folder: listing films, adding one (a copy, with progress), spotting duplicates.

"Add film" copies a film from anywhere (Downloads, a USB stick, another drive) into the
library folder (Settings > Playback > Library folder, normally "movies" next to
SyncWatch). The original stays where it was.

A copy is written to "<name>.part" first and only gets its real name once it's complete,
so a cancelled or failed copy never shows up as a broken film. Before copying, the free
space is checked (with some room to spare), and a film that's already in the library
isn't copied twice: same size and same content fingerprint (start, middle and end of
the file, so even huge films are compared in a moment).
"""
from __future__ import annotations

import hashlib
import os
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional, Tuple

VIDEO_EXTENSIONS = (".mp4", ".mkv", ".avi", ".mov", ".webm", ".m4v", ".wmv", ".mpg", ".mpeg", ".ts", ".m2ts",
                    ".ogv")
COPY_BLOCK = 8 * 1024 * 1024
FINGERPRINT_BLOCK = 1024 * 1024
SPACE_MARGIN = 200 * 1024 * 1024   # leave at least this much free after adding a film


class Cancelled(Exception):
    """The person clicked Cancel."""


@dataclass(frozen=True)
class Film:
    path: Path
    size: int
    modified: float

    @property
    def name(self) -> str:
        return self.path.name


def is_film(path: Path) -> bool:
    return path.suffix.lower() in VIDEO_EXTENSIONS and not path.name.startswith(".")


def list_films(folder: Path) -> List[Film]:
    """The films in the library folder (not in subfolders), sorted by name."""
    films = []
    try:
        entries = list(folder.iterdir())
    except OSError:
        return []
    for path in entries:
        if not is_film(path):
            continue
        try:
            info = path.stat()
        except OSError:
            continue
        if path.is_file():
            films.append(Film(path, info.st_size, info.st_mtime))
    return sorted(films, key=lambda film: film.name.casefold())


def is_inside(folder: Path, path: Path) -> bool:
    """Is ``path`` (directly or deeper) in ``folder``?"""
    try:
        path.resolve().relative_to(folder.resolve())
        return True
    except (ValueError, OSError):
        return False


def fingerprint(path: Path) -> str:
    """A quick content fingerprint: size plus 1 MB from the start, the middle and the end."""
    size = path.stat().st_size
    digest = hashlib.sha256(str(size).encode("ascii"))
    with open(path, "rb") as handle:
        for offset in sorted({0, max(0, size // 2 - FINGERPRINT_BLOCK // 2), max(0, size - FINGERPRINT_BLOCK)}):
            handle.seek(offset)
            digest.update(handle.read(FINGERPRINT_BLOCK))
    return digest.hexdigest()


def find_copy(folder: Path, source: Path) -> Optional[Path]:
    """A film in the library with the same content as ``source``, if there is one."""
    try:
        size = source.stat().st_size
    except OSError:
        return None
    candidates = [film for film in list_films(folder) if film.size == size]
    if not candidates:
        return None
    try:
        wanted = fingerprint(source)
    except OSError:
        return None
    for film in candidates:
        try:
            if fingerprint(film.path) == wanted:
                return film.path
        except OSError:
            continue
    return None


def unique_path(folder: Path, name: str) -> Path:
    """``folder/name``, or ``folder/name (2).ext`` etc. if that name is taken."""
    target = folder / name
    stem, suffix = Path(name).stem, Path(name).suffix
    number = 2
    while target.exists() or target.with_name(target.name + ".part").exists():
        target = folder / f"{stem} ({number}){suffix}"
        number += 1
    return target


def remove_stale_parts(folder: Path, older_than_s: float = 3600.0) -> List[Path]:
    """Delete half-finished copies ("<film>.part") left behind when SyncWatch was closed while
    copying or converting. Only SyncWatch's own kind, and only old ones (not one running now)."""
    removed = []
    try:
        entries = list(folder.iterdir())
    except OSError:
        return removed
    now = time.time()
    for path in entries:
        if path.suffix.lower() != ".part" or not is_film(path.with_suffix("")):
            continue
        try:
            if path.is_file() and now - path.stat().st_mtime > older_than_s:
                path.unlink()
                removed.append(path)
        except OSError:
            continue
    return removed


def space_problem(folder: Path, needed: int) -> Optional[Tuple[int, int]]:
    """(free, needed) if the library folder's drive doesn't have room for ``needed`` bytes."""
    folder.mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(folder).free
    if free < needed + SPACE_MARGIN:
        return free, needed + SPACE_MARGIN
    return None


def copy_film(source: Path, target: Path, progress: Callable[[int, int], None] = lambda done, total: None,
              cancelled: Callable[[], bool] = lambda: False) -> Path:
    """Copy ``source`` to ``target`` (via ``target.part``). Raises Cancelled or OSError.

    ``progress(done, total)`` is called after every 8 MB; ``cancelled()`` is asked just as often.
    """
    part = target.with_name(target.name + ".part")
    try:
        with open(source, "rb") as src, open(part, "wb") as dst:
            total = os.fstat(src.fileno()).st_size
            done = 0
            progress(0, total)
            while True:
                if cancelled():
                    raise Cancelled()
                block = src.read(COPY_BLOCK)
                if not block:
                    break
                dst.write(block)
                done += len(block)
                progress(done, total)
            dst.flush()
            os.fsync(dst.fileno())
        if done != total:
            raise OSError(f"copied {done} of {total} bytes (did the file change while copying?)")
        info = source.stat()
        os.utime(part, (info.st_atime, info.st_mtime))
        os.replace(part, target)
    except BaseException:
        try:
            part.unlink()
        except OSError:
            pass
        raise
    return target
