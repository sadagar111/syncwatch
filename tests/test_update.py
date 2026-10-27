"""Updates end to end: fake GitHub, fake installed SyncWatch, real signatures and the real boot.py."""
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402

from fake_github import FakeGitHub  # noqa: E402
from syncwatch.update import github, install, manifest  # noqa: E402
from syncwatch.update.layout import Layout  # noqa: E402
from syncwatch.update.manager import UpdateManager  # noqa: E402
from syncwatch.update.version import Version  # noqa: E402

REPO = "alex/syncwatch"
KEY = Ed25519PrivateKey.generate()
PUBLIC = manifest.public_key_text(KEY.public_key())
FAKE_MAIN = '''import sys, os, pathlib
root = pathlib.Path(__file__).resolve().parents[2]
version = "{version}"
with open(root / "ran.txt", "a") as f:
    f.write(version + " " + " ".join(sys.argv[1:]) + "\\n")
if (root / ("crash-" + version)).exists():
    raise RuntimeError("simulated crash in " + version)
'''


def app_files(version: str, extra=None) -> dict:
    files = {
        "syncwatch/__init__.py": f'__version__ = "{version}"\n',
        "syncwatch/__main__.py": FAKE_MAIN.format(version=version),
        "assets/logo.png": b"png",
        "_root/boot.py": (ROOT / "boot.py").read_bytes(),
    }
    files.update(extra or {})
    return files


def zip_bytes(files: dict) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, data in files.items():
            archive.writestr(name, data if isinstance(data, bytes) else data.encode())
    return buffer.getvalue()


def ref(name, data, tag=""):
    import hashlib
    return manifest.FileRef(name, len(data), hashlib.sha256(data).hexdigest(), tag)


def release(fake, version, runtime_id="r1", runtime_zip=None, key=KEY, prerelease=False, tamper=None,
            app_zip=None, repo=REPO):
    app_zip = app_zip if app_zip is not None else zip_bytes(app_files(version))
    assets = {f"SyncWatch-{version}-app.zip": app_zip}
    runtime_ref = None
    if runtime_zip is not None:
        assets[f"SyncWatch-runtime-{runtime_id}.zip"] = runtime_zip
        runtime_ref = ref(f"SyncWatch-runtime-{runtime_id}.zip", runtime_zip)
    data = manifest.build(version, repo, f"Notes for {version}", runtime_id,
                          ref(f"SyncWatch-{version}-app.zip", app_zip), runtime_ref, None, "2026-10-28T12:00:00Z")
    signature = manifest.sign(data, key)
    if tamper:
        data = tamper(data)
    assets["manifest.json"] = data
    assets["manifest.json.sig"] = signature
    fake.publish(f"v{version}", assets, prerelease=prerelease)


class Install:
    """A fake installed SyncWatch in a temporary folder."""

    def __init__(self, version="0.3.0"):
        self.root = Path(tempfile.mkdtemp(prefix="sw-install-"))
        shutil.copy(ROOT / "boot.py", self.root / "boot.py")
        (self.root / "runtime" / "r1").mkdir(parents=True)
        (self.root / "runtime" / "current.txt").write_text("r1")
        for name, data in app_files(version).items():
            path = self.root / "app" / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data if isinstance(data, bytes) else data.encode())
        self.layout = Layout(self.root / "app")

    def boot(self, *args, env=None):
        # SYNCWATCH_NO_POPUPS: on Windows, boot.py's error message box would wait forever for a click
        env = dict(os.environ if env is None else env, SYNCWATCH_NO_POPUPS="1")
        return subprocess.run([sys.executable, str(self.root / "boot.py"), *args], cwd=self.root,
                              capture_output=True, text=True, timeout=120, env=env)

    def ran(self):
        path = self.root / "ran.txt"
        return path.read_text().splitlines() if path.exists() else []

    def state(self):
        return self.layout.read_state()

    def close(self):
        shutil.rmtree(self.root, ignore_errors=True)


class VersionTests(unittest.TestCase):
    def test_order(self):
        order = ["0.2.9", "0.3.0", "0.3.1-test.1", "0.3.1-test.2", "0.3.1", "0.3.10", "1.0.0"]
        self.assertEqual(sorted(order, key=Version), order)
        self.assertTrue(Version("v0.3.1").is_test is False and Version("0.3.1-test.1").is_test)
        for bad in ("0.3", "0.3.1-beta", "x", "0.3.1-test"):
            with self.assertRaises(ValueError):
                Version(bad)


class ManifestTests(unittest.TestCase):
    def test_sign_verify_parse(self):
        app = b"app bytes"
        data = manifest.build("0.3.1", REPO, "notes", "r1", ref("a.zip", app), None, None, "now")
        signature = manifest.sign(data, KEY)
        self.assertEqual(manifest.verify_signature(data, signature, ["garbage", PUBLIC]), PUBLIC)
        parsed = manifest.parse(data, REPO)
        self.assertEqual(str(parsed.version), "0.3.1")
        self.assertEqual(parsed.channel, "stable")
        with self.assertRaises(manifest.ManifestError):
            manifest.verify_signature(data.replace(b"0.3.1", b"0.3.2"), signature, [PUBLIC])
        other = manifest.public_key_text(Ed25519PrivateKey.generate().public_key())
        with self.assertRaises(manifest.ManifestError):
            manifest.verify_signature(data, signature, [other])
        with self.assertRaises(manifest.ManifestError):
            manifest.verify_signature(data, b"not base64!!", [PUBLIC])
        with self.assertRaises(manifest.ManifestError):
            manifest.parse(data, "someone/else")

    def test_bad_fields(self):
        base = json.loads(manifest.build("0.3.1", REPO, "n", "r1", ref("a.zip", b"x"), None, None, "now"))
        for change in ({"app_zip": {"name": "../x.zip", "size": 1, "sha256": "0" * 64}},
                       {"app_zip": {"name": "a.zip", "size": -1, "sha256": "0" * 64}},
                       {"app_zip": {"name": "a.zip", "size": 1, "sha256": "zz"}},
                       {"channel": "test"}, {"format": 2}, {"runtime_id": "../../x"}, {"version": "1.0"}):
            bad = dict(base, **change)
            with self.assertRaises(manifest.ManifestError, msg=change):
                manifest.parse(json.dumps(bad).encode(), REPO)


class SafeExtractTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def extract(self, files):
        path = self.dir / "z.zip"
        path.write_bytes(zip_bytes(files))
        return install.safe_extract(path, self.dir / "out")

    def test_good(self):
        self.assertEqual(self.extract({"a/b.txt": "x", "c.txt": "y"}), 2)

    def test_zip_slip_and_absolute_paths(self):
        for name in ("../evil.txt", "a/../../evil.txt", "/abs.txt", "C:/win.txt", "a\\..\\..\\evil.txt"):
            shutil.rmtree(self.dir / "out", ignore_errors=True)
            with self.assertRaises(install.InstallError, msg=name):
                self.extract({name: "x"})
        self.assertFalse((self.dir.parent / "evil.txt").exists())

    def test_symlink_refused(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            info = zipfile.ZipInfo("link")
            info.external_attr = (0o120777 << 16)
            archive.writestr(info, "/etc/passwd")
        path = self.dir / "z.zip"
        path.write_bytes(buffer.getvalue())
        with self.assertRaises(install.InstallError):
            install.safe_extract(path, self.dir / "out")

    def test_damaged_zip(self):
        path = self.dir / "z.zip"
        path.write_bytes(b"PK\x03\x04 not really a zip")
        with self.assertRaises(install.InstallError):
            install.safe_extract(path, self.dir / "out")


class UpdateFlowTests(unittest.TestCase):
    def setUp(self):
        self.fake = FakeGitHub(REPO)
        self.install = Install("0.3.0")
        self.client = github.GitHub(REPO, api_base=self.fake.base, web_base=self.fake.base, timeout=5)

    def tearDown(self):
        self.fake.close()
        self.install.close()

    def manager(self, current="0.3.0", keys=None):
        return UpdateManager(self.install.layout, REPO, keys or [PUBLIC], current, self.client)

    def test_nothing_published(self):
        self.assertEqual(self.manager().check(False).status, "none_published")

    def test_full_update_then_boot_swaps_in(self):
        release(self.fake, "0.3.1")
        manager = self.manager()
        result = manager.check(False)
        self.assertEqual(result.status, "update")
        self.assertEqual(str(result.manifest.version), "0.3.1")
        progress = []
        manager.prepare(result, lambda d, t, s, e: progress.append((d, t)))
        self.assertEqual(progress[-1][0], progress[-1][1])
        self.assertEqual(manager.staged(), "0.3.1")
        out = self.install.boot("--profile", "Test")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(self.install.ran(), ["0.3.1 --profile Test"])
        self.assertEqual(self.install.layout.previous_version(), "0.3.0")
        state = self.install.state()
        self.assertEqual(state["just_updated"], {"version": "0.3.1", "from": "0.3.0"})
        news = UpdateManager(self.install.layout, REPO, [PUBLIC], "0.3.1", self.client).startup_news()
        self.assertEqual((news.updated_from, news.updated_to, news.notes), ("0.3.0", "0.3.1", "Notes for 0.3.1"))
        UpdateManager(self.install.layout, REPO, [PUBLIC], "0.3.1", self.client).mark_healthy()
        self.assertNotIn("just_updated", self.install.state())

    def test_already_downloaded_is_not_downloaded_again(self):
        release(self.fake, "0.3.1")
        manager = self.manager()
        result = manager.check(False)
        manager.prepare(result)
        before = len([r for r in self.fake.requests if r.endswith("-app.zip")])
        manager.prepare(result)
        after = len([r for r in self.fake.requests if r.endswith("-app.zip")])
        self.assertEqual(before, after)

    def test_broken_new_version_rolls_back_after_two_failed_starts(self):
        release(self.fake, "0.3.1")
        manager = self.manager()
        manager.prepare(manager.check(False))
        (self.install.root / "crash-0.3.1").touch()
        for _ in range(2):
            out = self.install.boot()
            self.assertNotEqual(out.returncode, 0)
        out = self.install.boot()  # third start: goes back to 0.3.0
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(self.install.ran()[-1].strip(), "0.3.0")
        state = self.install.state()
        self.assertEqual(state["rolled_back"], {"version": "0.3.1", "to": "0.3.0"})
        self.assertEqual(state["auto_skip"], "0.3.1")
        news = self.manager().startup_news()
        self.assertIn("SW-707", news.problems)

    def test_manual_rollback(self):
        release(self.fake, "0.3.1")
        manager = self.manager()
        manager.prepare(manager.check(False))
        self.install.boot()
        updated = UpdateManager(self.install.layout, REPO, [PUBLIC], "0.3.1", self.client)
        self.assertTrue(updated.request_rollback())
        self.install.boot()
        self.assertEqual(self.install.ran()[-1].strip(), "0.3.0")
        self.assertEqual(self.install.layout.previous_version(), "0.3.1")
        self.assertTrue(self.install.state()["rolled_back"]["manual"])

    def test_tampered_manifest_refused(self):
        release(self.fake, "0.3.1", tamper=lambda data: data.replace(b"Notes for", b"Evil notes for"))
        result = self.manager().check(False)
        self.assertEqual((result.status, result.code), ("error", "SW-703"))

    def test_wrong_key_refused(self):
        release(self.fake, "0.3.1", key=Ed25519PrivateKey.generate())
        self.assertEqual(self.manager().check(False).code, "SW-703")

    def test_manifest_for_another_repo_refused(self):
        release(self.fake, "0.3.1", repo="mallory/syncwatch")
        self.assertEqual(self.manager().check(False).code, "SW-703")

    def test_tag_mismatch_refused(self):
        release(self.fake, "0.3.1")
        self.fake.releases["v0.3.1"]["prerelease"] = True
        self.fake.publish("v0.9.9", self.fake.releases["v0.3.1"]["assets"], prerelease=True)
        result = self.manager().check(include_test=True)
        self.assertEqual((result.status, result.code), ("error", "SW-703"))

    def test_swapped_zip_refused(self):
        evil = zip_bytes(app_files("0.3.1", {"syncwatch/evil.py": "print('owned')"}))
        release(self.fake, "0.3.1")
        self.fake.releases["v0.3.1"]["assets"]["SyncWatch-0.3.1-app.zip"] = evil  # replaced after signing
        manager = self.manager()
        with self.assertRaises(github.ChecksumError):
            manager.prepare(manager.check(False))
        self.assertEqual(manager.staged(), "")

    def test_older_version_never_offered(self):
        release(self.fake, "0.2.9")
        self.assertEqual(self.manager().check(False).status, "current")

    def test_test_versions_only_when_wanted(self):
        release(self.fake, "0.3.1")
        release(self.fake, "0.3.2-test.1", prerelease=True)
        self.assertEqual(str(self.manager().check(False).manifest.version), "0.3.1")
        result = self.manager().check(True)
        self.assertEqual((result.status, str(result.manifest.version)), ("update", "0.3.2-test.1"))

    def test_skip_version(self):
        release(self.fake, "0.3.1")
        self.assertEqual(self.manager().check(False, skip_version="0.3.1").status, "skipped")
        self.assertEqual(self.manager().check(False, skip_version="0.3.1", manual=True).status, "update")

    def test_download_resumes_after_break(self):
        big = zip_bytes(app_files("0.3.1", {"assets/big.bin": os.urandom(3_000_000)}))
        release(self.fake, "0.3.1", app_zip=big)
        self.fake.drop_once_after = 1_000_000
        manager = self.manager()
        manager.prepare(manager.check(False))
        self.assertEqual(manager.staged(), "0.3.1")
        downloads = [r for r in self.fake.requests if r.endswith("-app.zip")]
        self.assertEqual(len(downloads), 2)  # broke off once, then resumed

    def test_rate_limit_and_offline(self):
        release(self.fake, "0.3.1", prerelease=True)
        self.fake.rate_limited = True
        self.assertEqual(self.manager().check(True).code, "SW-702")
        offline = github.GitHub(REPO, api_base="http://127.0.0.1:9", web_base="http://127.0.0.1:9", timeout=2)
        result = UpdateManager(self.install.layout, REPO, [PUBLIC], "0.3.0", offline).check(False)
        self.assertEqual(result.code, "SW-701")

    def test_not_configured_and_source_mode(self):
        self.assertEqual(UpdateManager(self.install.layout, "", [PUBLIC], "0.3.0", self.client).check(False).code, "SW-711")
        self.assertEqual(UpdateManager(self.install.layout, REPO, [], "0.3.0", self.client).availability().code, "SW-711")
        self.assertEqual(UpdateManager(Layout(ROOT), REPO, [PUBLIC], "0.3.0", self.client).availability().code, "SW-709")

    def _runtime_zip(self, script: str) -> bytes:
        """A stand-in runtime: bin/python3 is a small script (real runtimes are Windows Python folders)."""
        runtime_zip = io.BytesIO()
        with zipfile.ZipFile(runtime_zip, "w") as archive:
            archive.writestr("bin/python3", script)
        return runtime_zip.getvalue()

    def _stage_runtime(self, script: str):
        release(self.fake, "0.3.1", runtime_id="r2", runtime_zip=self._runtime_zip(script))
        manager = self.manager()
        result = manager.check(False)
        self.assertTrue(result.needs_runtime)
        manager.prepare(result)
        os.chmod(self.install.root / "runtime" / "r2" / "bin" / "python3", 0o755)
        self.assertEqual((self.install.root / "runtime" / "next.txt").read_text(), "r2")

    @unittest.skipIf(sys.platform == "win32", "uses a shell-script stand-in for the runtime (Linux/macOS)")
    def test_runtime_update_switches_after_test(self):
        self._stage_runtime(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n')
        out = self.install.boot()
        self.assertEqual(out.returncode, 0, out.stderr)
        deadline = time.time() + 30
        while time.time() < deadline and not self.install.ran():
            time.sleep(0.2)  # the relaunch on the new runtime runs detached
        self.assertEqual((self.install.root / "runtime" / "current.txt").read_text(), "r2")
        self.assertEqual((self.install.root / "runtime" / "previous.txt").read_text(), "r1")
        self.assertEqual(self.install.ran()[-1].strip(), "0.3.1")

    @unittest.skipIf(sys.platform == "win32", "uses a shell-script stand-in for the runtime (Linux/macOS)")
    def test_broken_runtime_is_refused(self):
        self._stage_runtime("#!/bin/sh\nexit 1\n")
        out = self.install.boot()
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual((self.install.root / "runtime" / "current.txt").read_text(), "r1")
        self.assertIn("didn't pass its test", self.install.state()["apply_error"])
        self.assertEqual(self.install.ran()[-1].strip(), "0.3.1")  # the app update itself still applied


if __name__ == "__main__":
    unittest.main()
