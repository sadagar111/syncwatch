"""Starts the installed SyncWatch and applies downloaded updates.

SyncWatch.bat runs:  runtime\\<id>\\pythonw.exe boot.py [arguments]

Before starting the app this:
1. switches to a newly downloaded runtime (SyncWatch's own Python), but only
   after test-starting it;
2. swaps a downloaded update (app.new) in, keeping the old version as app.previous;
3. goes back to the previous version if the new one failed to start twice in
   a row. The app tells boot.py it started fine by clearing "start_attempts"
   in update_state.json;
4. goes back to the previous version on request (Settings > Updates).

Rule for whoever edits this file: it must keep working with older app
versions, because an update replaces this file before the app itself is
swapped. Only the Python standard library may be used here.
"""
import json
import os
import shutil
import subprocess
import sys
import time
import traceback
from pathlib import Path

BOOT_VERSION = 1

ROOT = Path(__file__).resolve().parent
APP = ROOT / "app"
NEW = ROOT / "app.new"
PREVIOUS = ROOT / "app.previous"
FAILED = ROOT / "app.failed"
RUNTIME = ROOT / "runtime"
STATE = ROOT / "update_state.json"
LOG = ROOT / "boot.log"
MAX_FAILED_STARTS = 2
NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0          # CREATE_NO_WINDOW
DETACHED = (0x00000008 | 0x00000200) if sys.platform == "win32" else 0  # DETACHED_PROCESS | NEW_PROCESS_GROUP


def log(message):
    try:
        if LOG.exists() and LOG.stat().st_size > 200_000:
            LOG.unlink()
        with open(LOG, "a", encoding="utf-8") as handle:
            handle.write(time.strftime("%Y-%m-%d %H:%M:%S ") + message + "\n")
    except OSError:
        pass


def read_state():
    try:
        data = json.loads(STATE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def write_state(state):
    try:
        tmp = STATE.with_name(STATE.name + ".tmp")
        tmp.write_text(json.dumps(state, indent=1), encoding="utf-8")
        os.replace(tmp, STATE)
    except OSError as exc:
        log("could not write state: %s" % exc)


def version_of(app_dir):
    try:
        for line in (app_dir / "syncwatch" / "__init__.py").read_text(encoding="utf-8").splitlines():
            if line.startswith("__version__"):
                return line.split("=", 1)[1].strip().strip("\"'")
    except OSError:
        pass
    return "?"


def rename(source, target, attempts=20):
    """Rename with retries: right after SyncWatch closes, Windows or antivirus may still hold files."""
    for attempt in range(attempts):
        try:
            os.replace(str(source), str(target))
            return True
        except OSError as exc:
            if attempt == attempts - 1:
                log("rename %s -> %s failed: %s" % (source.name, target.name, exc))
            time.sleep(0.5)
    return False


def remove(path):
    for _ in range(5):
        if not path.exists():
            return True
        shutil.rmtree(str(path), ignore_errors=True)
        if not path.exists():
            return True
        time.sleep(0.5)
    if path.exists():  # still locked: move it aside so the name is free; deleted next time
        rename(path, path.with_name("%s.trash-%d" % (path.name, int(time.time()))), attempts=2)
    return not path.exists()


def clean_trash():
    for path in ROOT.glob("*.trash-*"):
        shutil.rmtree(str(path), ignore_errors=True)


# ---------------------------------------------------------------- runtime switching
def read_text(path):
    try:
        return path.read_text(encoding="ascii").strip()
    except OSError:
        return ""


def switch_runtime(state):
    """Returns True if SyncWatch was restarted on a new runtime (this process should exit)."""
    next_file = RUNTIME / "next.txt"
    if not next_file.is_file():
        return False
    new_id = read_text(next_file)
    try:
        next_file.unlink()
    except OSError:
        pass
    new_dir = RUNTIME / new_id
    current = read_text(RUNTIME / "current.txt")
    if not new_id or new_id == current or not (new_dir / ".ready").is_file():
        return False
    python = new_dir / ("python.exe" if sys.platform == "win32" else "bin/python3")
    try:
        subprocess.run([str(python), "-c", "import PySide6.QtCore, PySide6.QtMultimedia, cryptography"],
                       timeout=180, check=True, creationflags=NO_WINDOW,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError) as exc:
        log("new runtime %s failed its test: %s" % (new_id, exc))
        state["apply_error"] = "the new runtime %s didn't pass its test (%s)" % (new_id, exc)
        return False
    try:
        (RUNTIME / "previous.txt").write_text(current, encoding="ascii")
        (RUNTIME / "current.txt").write_text(new_id, encoding="ascii")
    except OSError as exc:
        state["apply_error"] = "couldn't switch runtime: %s" % exc
        return False
    log("switched runtime %s -> %s" % (current, new_id))
    write_state(state)
    launcher = new_dir / ("pythonw.exe" if sys.platform == "win32" else "bin/python3")
    subprocess.Popen([str(launcher), str(ROOT / "boot.py")] + sys.argv[1:], creationflags=DETACHED,
                     close_fds=True, cwd=str(ROOT), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL)
    return True


def clean_old_runtimes():
    keep = {read_text(RUNTIME / "current.txt"), read_text(RUNTIME / "previous.txt")}
    for path in RUNTIME.iterdir() if RUNTIME.is_dir() else []:
        if path.is_dir() and path.name not in keep and not path.name.endswith(".tmp"):
            shutil.rmtree(str(path), ignore_errors=True)  # fails harmlessly if still in use


# ---------------------------------------------------------------- app updates
def apply(state):
    if state.pop("rollback_requested", False) and (PREVIOUS / "syncwatch" / "__init__.py").is_file():
        swap = ROOT / "app.swap"
        remove(swap)
        current, previous = version_of(APP), version_of(PREVIOUS)
        if rename(APP, swap) and rename(PREVIOUS, APP) and rename(swap, PREVIOUS):
            state["rolled_back"] = {"version": current, "to": previous, "manual": True}
            state.pop("just_updated", None)
            state.pop("start_attempts", None)
            log("went back from %s to %s on request" % (current, previous))
        else:
            state["apply_error"] = "couldn't go back to the previous version"
        return

    if (NEW / ".ready").is_file():
        new_version, old_version = version_of(NEW), version_of(APP)
        if not remove(PREVIOUS):
            state["apply_error"] = "couldn't remove app.previous"
            return
        if not rename(APP, PREVIOUS):
            state["apply_error"] = "the current version's folder is in use"
            return
        if not rename(NEW, APP):
            rename(PREVIOUS, APP)
            state["apply_error"] = "couldn't move the new version into place"
            return
        try:
            (APP / ".ready").unlink()
        except OSError:
            pass
        state["just_updated"] = {"version": new_version, "from": old_version}
        state["start_attempts"] = 0
        state.pop("staged_version", None)
        log("updated %s -> %s" % (old_version, new_version))

    if state.get("just_updated"):
        attempts = int(state.get("start_attempts", 0)) + 1
        state["start_attempts"] = attempts
        if attempts > MAX_FAILED_STARTS and (PREVIOUS / "syncwatch" / "__init__.py").is_file():
            failed = version_of(APP)
            remove(FAILED)
            if rename(APP, FAILED) and rename(PREVIOUS, APP):
                state["rolled_back"] = {"version": failed, "to": version_of(APP)}
                state["auto_skip"] = failed  # don't offer the broken version again automatically
                state.pop("just_updated", None)
                state.pop("start_attempts", None)
                log("version %s failed to start %d times; went back to %s" % (failed, attempts - 1, version_of(APP)))


# ---------------------------------------------------------------- start
def fail(text):
    log("FATAL " + text)
    message = ("SyncWatch couldn't start (SW-712).\n\n%s\n\nWhat you can try:\n"
               "- Start SyncWatch again.\n"
               "- Download the full SyncWatch zip again and unzip it over this folder. "
               "Your settings are kept, they're stored separately.\n\nDetails: %s" % (text, LOG))
    if sys.platform == "win32" and not os.environ.get("SYNCWATCH_NO_POPUPS"):  # set by the tests
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, message, "SyncWatch", 0x10)
        except Exception:
            pass
    else:
        print(message, file=sys.stderr)


def main():
    if sys.stdout is None or sys.stderr is None:  # pythonw.exe has no console
        sys.stdout = sys.stderr = open(os.devnull, "w")
    clean_trash()
    state = read_state()
    try:
        if switch_runtime(state):
            return 0
        apply(state)
        clean_old_runtimes()
    except Exception:
        log("update step failed:\n" + traceback.format_exc())
        state["apply_error"] = "boot.py: " + traceback.format_exc().strip().splitlines()[-1]
    write_state(state)
    if not (APP / "syncwatch" / "__main__.py").is_file():
        fail("The app folder is missing or incomplete.")
        return 1
    sys.path.insert(0, str(APP))
    sys.argv = [str(APP / "syncwatch")] + sys.argv[1:]
    import runpy
    try:
        runpy.run_module("syncwatch", run_name="__main__", alter_sys=True)
    except SystemExit as exit_request:
        return exit_request.code if isinstance(exit_request.code, int) else 0
    except Exception:
        fail(traceback.format_exc().strip().splitlines()[-1])
        log(traceback.format_exc())
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
