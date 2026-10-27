"""Where SyncWatch keeps its files.

Two kinds of files exist:

* App files (code, ``assets``, the ``movies`` folder) live next to the app.
  Updates replace them.
* Your own files (settings, avatar, logs, saved positions) live in your user
  profile, e.g. ``%APPDATA%\\SyncWatch\\profiles\\default`` on Windows.
  Updates never touch them, and they can never end up in the GitHub repo.

A *profile* is one set of your own files. Normally there is only "default";
running a second window with ``--profile B`` lets you test watching together
on a single PC without the two windows overwriting each other's settings.
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent
ASSETS_DIR = APP_DIR / "assets"
# Installed version: the app sits in <SyncWatch>\app and gets replaced by updates, so the
# films folder is one level up, next to SyncWatch.bat. Source-code version: the project folder.
INSTALL_ROOT = (APP_DIR.parent if APP_DIR.name == "app" and (APP_DIR.parent / "boot.py").is_file()
                else APP_DIR)
DEFAULT_MOVIES_DIR = INSTALL_ROOT / "movies"
OLD_POSITIONS_FILE = APP_DIR / "positions.json"  # used by version 0.1

_PROFILE_NAME = re.compile(r"^[A-Za-z0-9_-]{1,32}$")


def data_root() -> Path:
    """Folder that holds all profiles. ``SYNCWATCH_DATA_DIR`` overrides it (tests)."""
    override = os.environ.get("SYNCWATCH_DATA_DIR")
    if override:
        return Path(override)
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    elif sys.platform == "darwin":
        base = str(Path.home() / "Library" / "Application Support")
    else:
        base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "SyncWatch"


def valid_profile_name(name: str) -> bool:
    return bool(_PROFILE_NAME.match(name or ""))


class Profile:
    """All per-user file locations for one profile."""

    def __init__(self, name: str = "default", root: Path | None = None):
        if not valid_profile_name(name):
            raise ValueError(f"invalid profile name: {name!r}")
        self.name = name
        self.dir = (root or data_root()) / "profiles" / name
        self.settings_file = self.dir / "settings.json"
        self.films_file = self.dir / "films.json"
        self.avatar_file = self.dir / "avatar.png"
        self.logs_dir = self.dir / "logs"
        self.cache_dir = self.dir / "cache"
        self.crash_file = self.dir / "logs" / "last_crash.txt"
        self.lock_file = self.dir / "running.lock"

    def ensure(self) -> None:
        """Create the folders and prove we can write there (raises OSError if not)."""
        for folder in (self.dir, self.logs_dir, self.cache_dir):
            folder.mkdir(parents=True, exist_ok=True)
        probe = self.dir / ".write_test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()


def movies_dir(configured: str) -> Path:
    """The films folder: the configured one if set, else ``movies`` next to the app."""
    if configured:
        return Path(configured).expanduser()
    return DEFAULT_MOVIES_DIR
