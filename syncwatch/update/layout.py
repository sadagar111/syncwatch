"""The folders of an installed SyncWatch, and telling "installed" from "source code".

Installed (the zip friends download)::

    SyncWatch\\
      SyncWatch.bat        starts runtime\\<id>\\pythonw.exe boot.py
      boot.py              swaps in staged updates, goes back if one fails
      update_state.json    what boot.py and the app tell each other
      runtime\\current.txt  which runtime folder to use, e.g. "r1"
      runtime\\r1\\         SyncWatch's own Python + libraries
      app\\                 the current version (syncwatch\\, assets\\, tools\\)
      app.previous\\        the version before (to go back to)
      app.new\\             a downloaded update, swapped in at the next start
      movies\\              your films (updates never touch it)

Source code (a Git folder, like yours): ``syncwatch`` sits directly in the
project folder. Updates are switched off there; use ``git pull`` (SW-709).
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, Optional

from .. import paths

STATE_FILE = "update_state.json"
READY_MARKER = ".ready"
BOOT_VERSION_FILE = "boot.py"


class Layout:
    def __init__(self, app_dir: Optional[Path] = None):
        self.app_dir = (app_dir or paths.APP_DIR).resolve()
        parent = self.app_dir.parent
        self.installed = (self.app_dir.name == "app" and (parent / "boot.py").is_file()
                          and (parent / "runtime").is_dir())
        self.root = parent if self.installed else self.app_dir

    # ------------------------------------------------------------ folders
    @property
    def app_new(self) -> Path:
        return self.root / "app.new"

    @property
    def app_previous(self) -> Path:
        return self.root / "app.previous"

    @property
    def runtime_dir(self) -> Path:
        return self.root / "runtime"

    @property
    def downloads(self) -> Path:
        return self.root / "downloads"

    @property
    def state_file(self) -> Path:
        return self.root / STATE_FILE

    def current_runtime(self) -> str:
        try:
            return (self.runtime_dir / "current.txt").read_text(encoding="ascii").strip()
        except OSError:
            return ""

    def has_previous(self) -> bool:
        return (self.app_previous / "syncwatch" / "__init__.py").is_file()

    def previous_version(self) -> str:
        return _read_version(self.app_previous)

    def staged_version(self) -> str:
        if not (self.app_new / READY_MARKER).is_file():
            return ""
        return _read_version(self.app_new)

    def boot_version(self) -> int:
        """The BOOT_VERSION number written in the installed boot.py (0 if unknown)."""
        try:
            for line in (self.root / "boot.py").read_text(encoding="utf-8").splitlines():
                if line.startswith("BOOT_VERSION"):
                    return int(line.split("=", 1)[1].strip())
        except (OSError, ValueError):
            pass
        return 0

    # ------------------------------------------------------------ shared state with boot.py
    def read_state(self) -> Dict[str, Any]:
        try:
            data = json.loads(self.state_file.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def write_state(self, data: Dict[str, Any]) -> bool:
        try:
            tmp = self.state_file.with_name(STATE_FILE + ".tmp")
            tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
            os.replace(tmp, self.state_file)
            return True
        except OSError:
            return False

    def update_state(self, **changes: Any) -> Dict[str, Any]:
        state = self.read_state()
        for key, value in changes.items():
            if value is None:
                state.pop(key, None)
            else:
                state[key] = value
        self.write_state(state)
        return state


def _read_version(app_dir: Path) -> str:
    try:
        for line in (app_dir / "syncwatch" / "__init__.py").read_text(encoding="utf-8").splitlines():
            if line.startswith("__version__"):
                return line.split("=", 1)[1].strip().strip("\"'")
    except OSError:
        pass
    return ""
