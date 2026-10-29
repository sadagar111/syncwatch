"""The release tool, end to end: keygen -> publish -> an installed app accepts the update.

Uses a temporary copy of the project as a Git repo with a local "origin", a fake
`gh` command (stores releases in a folder), and a stand-in runtime zip
(python.org isn't reachable in every test environment).
"""
import builtins
import importlib
import io
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
import zipfile
from pathlib import Path
from unittest import mock

SOURCE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))

FAKE_GH = textwrap.dedent('''\
    #!/usr/bin/env python3
    import fnmatch, json, os, shutil, sys
    from pathlib import Path
    state = Path(os.environ["FAKE_GH_DIR"])
    args = sys.argv[1:]
    def opt(name, default=None):
        return args[args.index(name) + 1] if name in args else default
    if args[:2] == ["repo", "view"]:
        print(os.environ["FAKE_GH_REPO"]); sys.exit(0)
    if args[:2] == ["release", "list"]:
        tags = sorted((p.name for p in state.iterdir() if p.is_dir()), key=lambda t: (state / t).stat().st_mtime, reverse=True)
        print(json.dumps([{"tagName": t} for t in tags])); sys.exit(0)
    if args[:2] == ["release", "view"]:
        folder = state / args[2]
        if not folder.is_dir():
            sys.exit(1)
        print(json.dumps({"assets": [{"name": p.name} for p in folder.iterdir() if not p.name.startswith(".")]})); sys.exit(0)
    if args[:2] == ["release", "create"]:
        folder = state / args[2]; folder.mkdir()
        if "--prerelease" in args:
            (folder / ".prerelease").touch()
        skip = {"--title", "--notes-file"}
        files, i = [], 3
        while i < len(args):
            if args[i] in skip:
                i += 2; continue
            if args[i].startswith("--"):
                i += 1; continue
            files.append(args[i]); i += 1
        for f in files:
            shutil.copy(f, folder / Path(f).name)
        sys.exit(0)
    if args[:2] == ["release", "download"]:
        folder, pattern, target = state / args[2], opt("--pattern"), Path(opt("--dir", "."))
        target.mkdir(parents=True, exist_ok=True)
        for p in folder.iterdir():
            if fnmatch.fnmatch(p.name, pattern):
                shutil.copy(p, target / p.name)
        sys.exit(0)
    print("fake gh: unsupported", args, file=sys.stderr); sys.exit(2)
''')


def git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@unittest.skipIf(sys.platform == "win32", "the fake 'gh' is a script Windows can't run by name; Linux CI covers this")
@unittest.skipUnless(shutil.which("git"), "needs git")
class ReleaseToolTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="sw-release-"))
        cls.work = cls.tmp / "repo"
        shutil.copytree(SOURCE, cls.work, ignore=shutil.ignore_patterns("dist", "__pycache__", ".git",
                                                                         "node_modules", ".wrangler"))
        init = cls.work / "syncwatch" / "__init__.py"
        text = init.read_text()
        current = text.split('__version__ = "')[1].split('"')[0]
        init.write_text(text.replace(f'__version__ = "{current}"', '__version__ = "0.3.0"'))
        changelog = cls.work / "CHANGELOG.md"
        changelog.write_text("# Changelog\n\n## 0.3.0 (test)\n- Updates!\n\n" + changelog.read_text().split("\n", 2)[2])
        (cls.work / "syncwatch" / "update" / "keys.py").write_text('"""keys"""\n\nTRUSTED_KEYS = [\n]\n')
        (cls.work / "syncwatch" / "update" / "source.py").write_text('GITHUB_REPO = ""\n')
        cls.origin = cls.tmp / "origin.git"
        git("init", "--bare", "-b", "main", str(cls.origin), cwd=cls.tmp)
        git("init", "-b", "main", cwd=cls.work)
        git("config", "user.email", "test@example.com", cwd=cls.work)
        git("config", "user.name", "Test", cwd=cls.work)
        git("add", ".", cwd=cls.work)
        git("commit", "-m", "start", cwd=cls.work)
        git("remote", "add", "origin", str(cls.origin), cwd=cls.work)
        git("push", "-u", "origin", "main", cwd=cls.work)
        cls.bin = cls.tmp / "bin"
        cls.bin.mkdir()
        (cls.bin / "gh").write_text(FAKE_GH)
        os.chmod(cls.bin / "gh", 0o755)
        cls.gh_state = cls.tmp / "gh"
        cls.gh_state.mkdir()
        cls.env = mock.patch.dict(os.environ, {
            "PATH": f"{cls.bin}{os.pathsep}{os.environ['PATH']}", "FAKE_GH_DIR": str(cls.gh_state),
            "FAKE_GH_REPO": "alex/syncwatch", "SYNCWATCH_DATA_DIR": str(cls.tmp / "data")})
        cls.env.start()
        sys.path.insert(0, str(cls.work / "tools"))
        sys.path.insert(0, str(cls.work))
        for name in list(sys.modules):
            if name in ("release", "build_runtime", "keywords") or name == "syncwatch" or name.startswith("syncwatch."):
                del sys.modules[name]
        cls.release = importlib.import_module("release")
        cls.passphrase = "correct horse battery"

    @classmethod
    def tearDownClass(cls):
        cls.env.stop()
        sys.path.remove(str(cls.work / "tools"))
        sys.path.remove(str(cls.work))
        for name in list(sys.modules):
            if name in ("release", "build_runtime", "keywords") or name == "syncwatch" or name.startswith("syncwatch."):
                del sys.modules[name]
        shutil.rmtree(cls.tmp, ignore_errors=True)

    # ------------------------------------------------------------ helpers
    def answers(self, func, *, inputs=None, words=None):
        """Run a release command with scripted typing."""
        inputs = list(inputs or [])

        def fake_input(prompt=""):
            if words is not None and prompt.strip().startswith("Word "):
                return words["words"][int(prompt.split()[1].rstrip(":")) - 1]
            return inputs.pop(0) if inputs else ""

        with mock.patch.object(builtins, "input", fake_input), \
                mock.patch("getpass.getpass", return_value=self.passphrase), \
                mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            code = func()
        return code, out.getvalue()

    def fake_runtime(self, out_dir: Path) -> Path:
        rid = self.release.build_runtime.runtime_id()
        path = out_dir / f"SyncWatch-runtime-{rid}.zip"
        out_dir.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("python.exe", b"MZ stand-in")
            archive.writestr("RUNTIME_ID", rid)
        return path

    # ------------------------------------------------------------ tests (run in order)
    def test_1_keygen_and_backup(self):
        captured = {}
        original = self.release.keywords.to_words

        def capture(seed):
            captured["words"] = original(seed)
            return captured["words"]

        # Wrong answers in the "write it down" check: refused, nothing saved.
        with mock.patch.object(self.release.keywords, "to_words", capture):
            code, out = self.answers(lambda: self.release.main(["keygen"]), inputs=["", "nope", "nope", "nope"])
        self.assertEqual(code, 1, out)
        self.assertIn("no key was saved", out)
        self.assertFalse(self.release.key_file().exists())
        # Right answers: saved, and the public key lands in keys.py.
        with mock.patch.object(self.release.keywords, "to_words", capture):
            code, out = self.answers(lambda: self.release.main(["keygen"]), inputs=[""], words=captured)
        self.assertEqual(code, 0, out)
        self.assertTrue(self.release.key_file().exists())
        public = self.release.pub_file().read_text().strip()
        self.assertEqual(self.release.read_trusted_keys(), [public])
        words = captured["words"]
        # Backup check: the first 4 letters of each word are enough; swapped words are caught.
        code, out = self.answers(lambda: self.release.main(["check-backup"]), inputs=[" ".join(w[:4] for w in words)])
        self.assertEqual(code, 0, out)
        self.assertIn("Your 24 words are correct", out)
        wrong = list(words)
        wrong[5], wrong[6] = wrong[6], wrong[5]
        code, out = self.answers(lambda: self.release.main(["check-backup"]),
                                 inputs=[" ".join(wrong), " ".join(words)])
        self.assertIn("don't fit together", out)
        # A second keygen is refused (it would replace the key).
        code, out = self.answers(lambda: self.release.main(["keygen"]))
        self.assertEqual(code, 1)
        self.assertIn("already have a signing key", out)

    def test_2b_show_words_matches(self):
        code, out = self.answers(lambda: self.release.main(["show-words"]))
        self.assertEqual(code, 0, out)
        import re
        numbered = dict((int(n), w) for n, w in re.findall(r"(\d+)\. ([a-z]+)", out))  # printed in columns
        shown = [numbered[i] for i in range(1, 25)]
        public = self.release.pub_file().read_text().strip()
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        seed = self.release.keywords.from_words(shown)
        self.assertEqual(self.release.manifest.public_key_text(Ed25519PrivateKey.from_private_bytes(seed).public_key()),
                         public)

    def test_2_wrong_passphrase(self):
        with mock.patch("getpass.getpass", return_value="wrong passphrase!"):
            with self.assertRaises(self.release.Stop):
                self.release.load_key(b"wrong passphrase!")

    def test_3_set_repo_and_commit(self):
        code, out = self.answers(lambda: self.release.main(["set-repo", "not a repo"]))
        self.assertEqual(code, 1)
        code, out = self.answers(lambda: self.release.main(["set-repo", "alex/syncwatch"]))
        self.assertEqual(code, 0)
        # The relay address: checked against a running relay, refused when it's not one.
        sys.path.insert(0, str(SOURCE))
        from tools.local_relay import LocalRelay
        relay = LocalRelay().start()
        try:
            code, out = self.answers(lambda: self.release.main(["set-relay", "ftp://relay.example.org"]))
            self.assertEqual(code, 1)
            code, out = self.answers(lambda: self.release.main(["set-relay", "http://relay.example.org"]))
            self.assertEqual(code, 1)  # only https (http only for this PC, for testing)
            code, out = self.answers(lambda: self.release.main(["set-relay", relay.url]))
            self.assertEqual(code, 0, out)
            self.assertIn(f'RELAY_URL = "{relay.url}"', (self.work / "syncwatch" / "net" / "relay_url.py").read_text())
        finally:
            relay.stop()
        code, out = self.answers(lambda: self.release.main(["set-relay", "https://relay.example.invalid"]))
        self.assertEqual(code, 1)  # doesn't answer
        code, out = self.answers(lambda: self.release.main(["set-relay", "https://relay.example.invalid/",
                                                            "--no-check"]))
        self.assertEqual(code, 0)
        self.assertIn('RELAY_URL = "https://relay.example.invalid"',
                      (self.work / "syncwatch" / "net" / "relay_url.py").read_text())
        git("add", ".", cwd=self.work)
        git("commit", "-m", "key and repo", cwd=self.work)
        git("push", cwd=self.work)

    def test_4_dry_run(self):
        with mock.patch.object(self.release.build_runtime, "build", self.fake_runtime):
            code, out = self.answers(lambda: self.release.main(["publish", "--dry-run", "--skip-tests"]))
        self.assertEqual(code, 0, out)
        out_dir = self.work / "dist" / "v0.3.0"
        app = zipfile.ZipFile(out_dir / "SyncWatch-0.3.0-app.zip").namelist()
        self.assertIn("_root/boot.py", app)
        self.assertIn("_root/README.txt", app)
        self.assertIn("syncwatch/__main__.py", app)
        self.assertIn("tools/remove_firewall.bat", app)
        self.assertIn("tools/Test with two windows.bat", app)
        self.assertIn("tools/measure_jumps.bat", app)       # friends can measure their PCs too
        self.assertIn("syncwatch/room/session.py", app)
        self.assertIn("syncwatch/net/relay_url.py", app)
        self.assertNotIn("tools/release.py", app)
        self.assertNotIn("tools/local_relay.py", app)
        self.assertNotIn("tools/bots.py", app)          # testing tools stay in the Git folder
        self.assertFalse(any("__pycache__" in n for n in app))
        self.assertFalse(any(n.startswith("relay/") for n in app))
        self.assertFalse(any(n.startswith("tests/") for n in app))
        full = zipfile.ZipFile(out_dir / "SyncWatch-0.3.0-windows.zip").namelist()
        rid = self.release.build_runtime.runtime_id()
        for name in ("SyncWatch/SyncWatch.bat", "SyncWatch/boot.py", "SyncWatch/app/syncwatch/__init__.py",
                     "SyncWatch/runtime/current.txt", f"SyncWatch/runtime/{rid}/python.exe",
                     "SyncWatch/THIRD_PARTY_NOTICES.txt", "SyncWatch/movies/PUT_FILMS_HERE.txt"):
            self.assertIn(name, full)
        from syncwatch.update import manifest
        data = (out_dir / "manifest.json").read_bytes()
        manifest.verify_signature(data, (out_dir / "manifest.json.sig").read_bytes(), self.release.read_trusted_keys())
        parsed = manifest.parse(data, "alex/syncwatch")
        self.assertEqual(parsed.notes, "- Updates!")
        self.assertEqual(parsed.runtime_id, rid)
        # building twice gives byte-identical app zips (reproducible)
        first = (out_dir / "SyncWatch-0.3.0-app.zip").read_bytes()
        with mock.patch.object(self.release.build_runtime, "build", self.fake_runtime):
            self.answers(lambda: self.release.main(["publish", "--dry-run", "--skip-tests"]))
        self.assertEqual(first, (out_dir / "SyncWatch-0.3.0-app.zip").read_bytes())

    def test_5_refusals(self):
        (self.work / "uncommitted.txt").write_text("x")
        with mock.patch.object(self.release.build_runtime, "build", self.fake_runtime):
            code, out = self.answers(lambda: self.release.main(["publish", "--skip-tests"]))
        self.assertIn("aren't committed", out)
        (self.work / "uncommitted.txt").unlink()
        code, out = self.answers(lambda: self.release.main(["publish", "--test", "--skip-tests"]))
        self.assertIn("is not a test version", out)
        # Rooms without a relay address: refused (friends' rooms would be off).
        relay_file = self.work / "syncwatch" / "net" / "relay_url.py"
        kept = relay_file.read_text()
        relay_file.write_text('RELAY_URL = ""\n')
        git("commit", "-am", "no relay", cwd=self.work)
        try:
            with mock.patch.object(self.release.build_runtime, "build", self.fake_runtime):
                code, out = self.answers(lambda: self.release.main(["publish", "--skip-tests"]))
            self.assertEqual(code, 1)
            self.assertIn("Rooms need a relay server", out)
        finally:
            relay_file.write_text(kept)
            git("commit", "-am", "relay back", cwd=self.work)
            git("push", cwd=self.work)

    def test_6_publish_and_app_accepts_it(self):
        with mock.patch.object(self.release.build_runtime, "build", self.fake_runtime):
            code, out = self.answers(lambda: self.release.main(["publish", "--skip-tests"]), inputs=["0.3.0"])
        self.assertEqual(code, 0, out)
        published = sorted(p.name for p in (self.gh_state / "v0.3.0").iterdir())
        rid = self.release.build_runtime.runtime_id()
        self.assertEqual(published, sorted(["manifest.json", "manifest.json.sig", "SyncWatch-0.3.0-app.zip",
                                            "SyncWatch-0.3.0-windows.zip", f"SyncWatch-runtime-{rid}.zip"]))
        tags = subprocess.run(["git", "ls-remote", "--tags", str(self.origin)], capture_output=True, text=True).stdout
        self.assertIn("refs/tags/v0.3.0", tags)
        self.assertEqual(self.app_check("0.2.9"), ("update", "0.3.0", True))

    def test_7_second_release_reuses_runtime(self):
        init = self.work / "syncwatch" / "__init__.py"
        init.write_text(init.read_text().replace('"0.3.0"', '"0.3.1"'))
        changelog = self.work / "CHANGELOG.md"
        changelog.write_text(changelog.read_text().replace("## 0.3.0", "## 0.3.1\n- A fix\n\n## 0.3.0", 1))
        git("commit", "-am", "0.3.1", cwd=self.work)
        git("push", cwd=self.work)
        built = []
        with mock.patch.object(self.release.build_runtime, "build", lambda d: built.append(d) or self.fake_runtime(d)):
            code, out = self.answers(lambda: self.release.main(["publish", "--skip-tests", "--no-full-zip"]),
                                     inputs=["0.3.1"])
        self.assertEqual(code, 0, out)
        self.assertEqual(built, [], "the runtime should be reused, not rebuilt")
        published = sorted(p.name for p in (self.gh_state / "v0.3.1").iterdir())
        self.assertEqual(published, ["SyncWatch-0.3.1-app.zip", "manifest.json", "manifest.json.sig"])
        status, version, needs_runtime = self.app_check("0.3.0", runtime=self.release.build_runtime.runtime_id())
        self.assertEqual((status, version, needs_runtime), ("update", "0.3.1", False))
        # a lower version can't be published
        code, out = self.answers(lambda: self.release.main(["publish", "--skip-tests"]), inputs=["0.3.1"])
        self.assertIn("already has a tag", out)

    # ------------------------------------------------------------ the app's side
    def app_check(self, installed_version, runtime="old-runtime"):
        from fake_github import FakeGitHub
        from syncwatch.update import github
        from syncwatch.update.layout import Layout
        from syncwatch.update.manager import UpdateManager
        fake = FakeGitHub("alex/syncwatch")
        install_root = self.tmp / f"install-{installed_version}"
        try:
            for folder in sorted(self.gh_state.iterdir(), key=lambda p: p.stat().st_mtime):
                fake.publish(folder.name, {p.name: p.read_bytes() for p in folder.iterdir() if not p.name.startswith(".")},
                             prerelease=(folder / ".prerelease").exists())
            (install_root / "app" / "syncwatch").mkdir(parents=True)
            (install_root / "app" / "syncwatch" / "__init__.py").write_text(f'__version__ = "{installed_version}"\n')
            (install_root / "boot.py").write_text("BOOT_VERSION = 1\n")
            (install_root / "runtime").mkdir()
            (install_root / "runtime" / "current.txt").write_text(runtime)
            client = github.GitHub("alex/syncwatch", api_base=fake.base, web_base=fake.base, timeout=5)
            manager = UpdateManager(Layout(install_root / "app"), "alex/syncwatch",
                                    self.release.read_trusted_keys(), installed_version, client)
            result = manager.check(False)
            if result.status == "update":
                manager.prepare(result)
                self.assertEqual(manager.staged(), str(result.manifest.version))
            return result.status, str(result.manifest.version) if result.manifest else "", result.needs_runtime
        finally:
            fake.close()
            shutil.rmtree(install_root, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
