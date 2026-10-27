"""Unpacking downloaded updates safely and getting them ready for the next start.

Even though files are signed and fingerprint-checked before they get here,
unpacking is still defensive (defence in depth): every path in a zip is
checked so nothing can be written outside the target folder ("zip slip"),
links are refused, and the total size and number of files are capped.
"""
from __future__ import annotations

import logging
import os
import shutil
import stat
import time
import zipfile
from pathlib import Path, PurePosixPath
from typing import Optional

from .layout import READY_MARKER, Layout, _read_version

log = logging.getLogger("syncwatch.update")

MAX_TOTAL_BYTES = 3 * 1024 ** 3
MAX_FILES = 100_000
ROOT_FILES_DIR = "_root"     # inside the app zip: files that belong next to SyncWatch.bat
ROOT_FILES = ("boot.py", "SyncWatch.bat", "README.txt")


class InstallError(Exception):
    """Unpacking or staging failed (SW-706 / SW-710)."""

    def __init__(self, code: str, technical: str):
        super().__init__(technical)
        self.code = code
        self.technical = technical


def _safe_name(name: str) -> Optional[PurePosixPath]:
    """A zip entry name as a safe relative path, or None if it must be refused."""
    name = name.replace("\\", "/")
    if not name or name.startswith("/") or ":" in name or "\x00" in name:
        return None
    path = PurePosixPath(name)
    if any(part in ("..", "") for part in path.parts if part != ".") or path.is_absolute():
        return None
    return path


def safe_extract(zip_path: Path, target: Path) -> int:
    """Extract into ``target`` (which must not exist yet). Returns the number of files."""
    total, count = 0, 0
    try:
        with zipfile.ZipFile(zip_path) as archive:
            members = archive.infolist()
            if len(members) > MAX_FILES:
                raise InstallError("SW-706", f"zip has too many entries ({len(members)})")
            for info in members:
                if _safe_name(info.filename) is None:
                    raise InstallError("SW-706", f"unsafe path in zip: {info.filename!r}")
                mode = info.external_attr >> 16
                if stat.S_ISLNK(mode):
                    raise InstallError("SW-706", f"link in zip: {info.filename!r}")
                total += info.file_size
                if total > MAX_TOTAL_BYTES:
                    raise InstallError("SW-706", "zip unpacks to more than 3 GB")
            target.mkdir(parents=True)
            root = target.resolve()
            for info in members:
                relative = _safe_name(info.filename)
                destination = (target / Path(*relative.parts)).resolve()
                if root not in destination.parents and destination != root:
                    raise InstallError("SW-706", f"path escapes target: {info.filename!r}")
                if info.is_dir():
                    destination.mkdir(parents=True, exist_ok=True)
                    continue
                destination.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(info) as source, open(destination, "wb") as out:
                    shutil.copyfileobj(source, out, 1 << 20)
                count += 1
    except zipfile.BadZipFile as exc:
        raise InstallError("SW-706", f"damaged zip: {exc}") from exc
    except OSError as exc:
        code = "SW-710" if getattr(exc, "errno", None) == 28 else "SW-706"  # 28 = no space left
        raise InstallError(code, f"{type(exc).__name__}: {exc}") from exc
    return count


def remove_tree(path: Path, attempts: int = 5) -> bool:
    """Delete a folder, retrying briefly (Windows antivirus often holds files for a moment)."""
    def make_writable(func, failed_path, _exc):
        try:
            os.chmod(failed_path, stat.S_IWRITE)
            func(failed_path)
        except OSError:
            pass

    for attempt in range(attempts):
        if not path.exists():
            return True
        try:
            shutil.rmtree(path, onerror=make_writable)
        except OSError:
            pass
        if not path.exists():
            return True
        time.sleep(0.3 * (attempt + 1))
    return not path.exists()


def check_space(folder: Path, needed: int) -> None:
    try:
        free = shutil.disk_usage(folder).free
    except OSError:
        return
    if free < needed:
        raise InstallError("SW-710", f"{free // 1_000_000} MB free, {needed // 1_000_000} MB needed")


def stage_app(layout: Layout, zip_path: Path, version: str) -> None:
    """Unpack an app zip into app.new and mark it ready for the next start."""
    tmp = layout.root / "app.new.tmp"
    remove_tree(tmp)
    if not remove_tree(layout.app_new):
        raise InstallError("SW-706", "couldn't remove an older app.new folder")
    safe_extract(zip_path, tmp)
    found = _read_version(tmp)
    if found != version:
        remove_tree(tmp)
        raise InstallError("SW-706", f"zip contains version {found!r}, expected {version!r}")
    try:
        os.replace(tmp, layout.app_new)
        (layout.app_new / READY_MARKER).write_text(version, encoding="utf-8")
    except OSError as exc:
        raise InstallError("SW-706", f"{type(exc).__name__}: {exc}") from exc
    _copy_root_files(layout)


def _copy_root_files(layout: Layout) -> None:
    """boot.py, SyncWatch.bat and README.txt live next to the app; neither is running now."""
    source_dir = layout.app_new / ROOT_FILES_DIR
    for name in ROOT_FILES:
        source = source_dir / name
        target = layout.root / name
        if not source.is_file():
            continue
        try:
            if target.is_file() and target.read_bytes() == source.read_bytes():
                continue
            tmp = target.with_name(name + ".tmp")
            shutil.copyfile(source, tmp)
            os.replace(tmp, target)
            log.info("updated %s", name)
        except OSError as exc:
            raise InstallError("SW-706", f"couldn't update {name}: {exc}") from exc


def stage_runtime(layout: Layout, zip_path: Path, runtime_id: str) -> None:
    """Unpack a runtime zip into runtime/<id> and ask boot.py to switch to it."""
    target = layout.runtime_dir / runtime_id
    tmp = layout.runtime_dir / f"{runtime_id}.tmp"
    remove_tree(tmp)
    if target.exists() and not (target / READY_MARKER).is_file():
        remove_tree(target)
    if not target.exists():
        safe_extract(zip_path, tmp)
        if not (tmp / "python.exe").is_file() and not (tmp / "bin").exists():
            remove_tree(tmp)
            raise InstallError("SW-706", "runtime zip has no python.exe")
        try:
            os.replace(tmp, target)
            (target / READY_MARKER).write_text(runtime_id, encoding="utf-8")
        except OSError as exc:
            raise InstallError("SW-706", f"{type(exc).__name__}: {exc}") from exc
    try:
        (layout.runtime_dir / "next.txt").write_text(runtime_id, encoding="ascii")
    except OSError as exc:
        raise InstallError("SW-706", f"{type(exc).__name__}: {exc}") from exc
