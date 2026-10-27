"""Whole update cycle with the real app: installed 0.3.0 finds 0.3.1, downloads it, restarts into it.

    QT_QPA_PLATFORM=offscreen python tests/update_e2e.py <screenshot folder>

Builds a fake installed SyncWatch in a temporary folder (a copy of this source
tree), publishes a signed "next version" on a local fake GitHub, then drives
the real window: wait for the banner, open the update window, click Update,
click Restart. Finally it checks that boot.py started the new version and
that the new version reports "Updated to ...". Exit code 0 = all good.
"""
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path

SOURCE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
OUT = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(tempfile.mkdtemp())
OUT.mkdir(parents=True, exist_ok=True)

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402

from fake_github import FakeGitHub  # noqa: E402

REPO = "alex/syncwatch"
KEY = Ed25519PrivateKey.generate()


def tree(version: str, public: str) -> dict:
    """The app files of a version, as {relative path: bytes}."""
    files = {}
    for folder in ("syncwatch", "assets", "tools"):
        for path in (SOURCE / folder).rglob("*"):
            if path.is_file() and "__pycache__" not in path.parts:
                files[path.relative_to(SOURCE).as_posix()] = path.read_bytes()
    init = files["syncwatch/__init__.py"].decode()
    current = init.split('__version__ = "')[1].split('"')[0]
    files["syncwatch/__init__.py"] = init.replace(f'__version__ = "{current}"', f'__version__ = "{version}"').encode()
    files["syncwatch/update/source.py"] = f'GITHUB_REPO = "{REPO}"\n'.encode()
    files["syncwatch/update/keys.py"] = f'TRUSTED_KEYS = ["{public}"]\n'.encode()
    files["_root/boot.py"] = (SOURCE / "boot.py").read_bytes()
    return files


def main() -> int:
    sys.path.insert(0, str(SOURCE))
    from syncwatch.update import manifest
    public = manifest.public_key_text(KEY.public_key())
    work = Path(tempfile.mkdtemp(prefix="sw-e2e-"))
    install, data = work / "SyncWatch", work / "data"
    for name, content in tree("0.3.0", public).items():
        target = install / "app" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    shutil.copy(SOURCE / "boot.py", install / "boot.py")
    (install / "runtime" / "r1").mkdir(parents=True)
    (install / "runtime" / "current.txt").write_text("r1")
    profile = data / "profiles" / "default"
    profile.mkdir(parents=True)
    (profile / "settings.json").write_text(json.dumps({"_version": 1, "values": {
        "profile.name": "Alex", "ui.startup_checks": "never", "ui.video_selftest": False}}))

    fake = FakeGitHub(REPO)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in tree("0.3.1", public).items():
            archive.writestr(name, content)
    app_zip = buffer.getvalue()
    import hashlib
    ref = manifest.FileRef("SyncWatch-0.3.1-app.zip", len(app_zip), hashlib.sha256(app_zip).hexdigest())
    data_bytes = manifest.build("0.3.1", REPO, "## What's new\n- Something **better**\n- A fix", "r1", ref,
                                None, None, "2026-10-28T12:00:00Z")
    fake.publish("v0.3.1", {"SyncWatch-0.3.1-app.zip": app_zip, "manifest.json": data_bytes,
                            "manifest.json.sig": manifest.sign(data_bytes, KEY)})

    env = dict(os.environ, SYNCWATCH_DATA_DIR=str(data), SYNCWATCH_UPDATE_SERVER=fake.base,
               QT_QPA_PLATFORM="offscreen", PYTHONPATH="", SYNCWATCH_TEST_EXIT_AFTER="40")
    driver = work / "driver.py"
    driver.write_text(DRIVER.replace('"@OUT@"', repr(str(OUT))))  # repr: Windows backslashes stay intact
    first = subprocess.run([sys.executable, str(driver)], cwd=install, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=180)
    print(first.stdout)
    if first.returncode != 0:
        print(first.stderr[-3000:])
    # boot.py restarted SyncWatch detached; wait for the new version to come up
    log = profile / "logs" / "syncwatch.log"
    deadline = time.time() + 60
    started_new = False
    while time.time() < deadline:
        text = log.read_text(errors="replace") if log.exists() else ""
        if "SyncWatch 0.3.1 starting" in text:
            started_new = True
            time.sleep(4)
            break
        time.sleep(0.5)
    deadline = time.time() + 60  # the restarted app closes itself (SYNCWATCH_TEST_EXIT_AFTER)
    while time.time() < deadline:
        text = log.read_text(errors="replace") if log.exists() else ""
        if text.count("SyncWatch closed") >= 2:
            break
        time.sleep(0.5)
    fake.close()
    def read(path: Path) -> str:
        return path.read_text(encoding="utf-8", errors="replace") if path.exists() else f"(missing: {path.name})"

    state_path = install / "update_state.json"
    state = json.loads(state_path.read_text()) if state_path.exists() else {}
    text = log.read_text(errors="replace") if log.exists() else ""
    checks = {
        "driver ok": first.returncode == 0 and "DRIVER OK" in first.stdout,
        "new version started": started_new,
        "app is 0.3.1": 'version__ = "0.3.1"' in read(install / "app" / "syncwatch" / "__init__.py"),
        "previous kept": 'version__ = "0.3.0"' in read(install / "app.previous" / "syncwatch" / "__init__.py"),
        "boot recorded update": "updated 0.3.0 -> 0.3.1" in read(install / "boot.log"),
        "new version marked healthy": state.get("healthy_version") == "0.3.1" and "just_updated" not in state,
        "no errors in log": " ERROR " not in text,
        "new version closed cleanly": text.count("SyncWatch closed") >= 2,
    }
    print("CHECKS:", checks)
    if not all(checks.values()):
        print("boot.log:\n" + read(install / "boot.log")[-2000:])
        print("app log (end):\n" + "\n".join(text.splitlines()[-40:]))
    if " ERROR " in text:
        print("\n".join(l for l in text.splitlines() if " ERROR " in l)[-2000:])
    shutil.rmtree(work, ignore_errors=True)
    return 0 if all(checks.values()) else 1


DRIVER = r'''
import sys, time
from pathlib import Path
root = Path.cwd()
sys.path.insert(0, str(root / "app"))
from PySide6.QtWidgets import QApplication
from syncwatch import app as app_module
OUT = Path("@OUT@")

def spin(seconds, until=None):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        QApplication.processEvents()
        if until and until():
            return True
        time.sleep(0.02)
    return False

def scripted(self):
    from syncwatch.ui.main_window import MainWindow
    w = next(x for x in QApplication.topLevelWidgets() if isinstance(x, MainWindow))
    ok = spin(20, lambda: w.updates.result is not None)
    print("update found:", ok, w.updates.result and str(w.updates.result.manifest.version))
    spin(0.3)
    w.grab().save(str(OUT / "e2e_1_banner.png"))
    w.updates.open_dialog()
    spin(0.5)
    w.updates.dialog.grab().save(str(OUT / "e2e_2_whats_new.png"))
    w.updates.dialog.updateClicked.emit()
    spin(30, lambda: w.updates.dialog.pages.currentIndex() == 2)
    w.updates.dialog.grab().save(str(OUT / "e2e_3_ready.png"))
    print("staged:", w.updates.manager.staged())
    w.grab().save(str(OUT / "e2e_4_banner_ready.png"))
    if w.updates.manager.staged() == "0.3.1":
        print("DRIVER OK")
    w.updates.dialog.restartClicked.emit()
    spin(1.0)
    return 0

QApplication.exec = scripted
sys.exit(app_module.main([]))
'''

if __name__ == "__main__":
    sys.exit(main())
