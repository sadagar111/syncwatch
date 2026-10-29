"""Fetches libmpv-2.dll (SyncWatch's video player library) into the project folder.

    python tools/get_libmpv.py

Only for the source-code version (a Git folder) on Windows: SyncWatch.bat runs this
once by itself. The zip for friends already has it inside (runtime\\<id>\\libmpv).
Downloads about 50 MB, checks the DLL's SHA-256 against runtime-spec/runtime.json and
puts it in libmpv\\ (Git ignores that folder). Running it again does nothing if the
right DLL is already there.

On Linux and macOS SyncWatch uses the system's libmpv instead (e.g. apt install libmpv2),
so this does nothing there unless you add --force.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build_runtime  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


def main(argv) -> int:
    if sys.platform != "win32" and "--force" not in argv:
        print("Not needed here: on Linux and macOS SyncWatch uses the system's libmpv "
              "(e.g. sudo apt install libmpv2).")
        return 0
    print("Getting SyncWatch's video player library (libmpv-2.dll, about 50 MB, only once)…")
    try:
        build_runtime.libmpv_dll(build_runtime.spec(), ROOT / "libmpv")
    except (build_runtime.BuildError, OSError) as exc:
        print(f"\nERROR: {exc}\nSyncWatch still works, with its previous video player (SW-209).")
        return 1
    print("Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
