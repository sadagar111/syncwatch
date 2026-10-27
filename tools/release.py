"""Publishing SyncWatch releases, and managing your signing key.

Run from the project folder (or use tools\\release.bat):

    python tools/release.py keygen          once: create your signing key (24 words + passphrase)
    python tools/release.py check-backup    type your 24 words to check the paper copy
    python tools/release.py restore         rebuild the key file from your 24 words (new PC, lost file)
    python tools/release.py show-words      show your 24 words again (needs the passphrase; lost paper)
    python tools/release.py set-repo OWNER/NAME
    python tools/release.py publish         publish the version in syncwatch/__init__.py
        --test          as a test version (version must look like 0.3.1-test.1)
        --dry-run       build and sign everything into dist/, but don't publish
        --skip-tests    don't run the automatic tests first (not recommended)
        --full-zip / --no-full-zip   include the all-in-one zip for new friends
                        (default: yes for normal versions, no for test versions)

Your signing key lives only on this PC, in %APPDATA%\\SyncWatch\\release, locked
with your passphrase, and on paper as 24 words. It never goes into the
project folder or onto GitHub.
"""
from __future__ import annotations

import argparse
import ast
import getpass
import hashlib
import io
import json
import os
import random
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import zipfile
from pathlib import Path
from typing import Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402

import build_runtime  # noqa: E402
import keywords  # noqa: E402
from syncwatch import paths  # noqa: E402
from syncwatch.update import manifest  # noqa: E402
from syncwatch.update.version import Version  # noqa: E402

KEYS_FILE = ROOT / "syncwatch" / "update" / "keys.py"
SOURCE_FILE = ROOT / "syncwatch" / "update" / "source.py"
FIXED_DATE = (2020, 1, 1, 0, 0, 0)
MIN_PASSPHRASE = 10

# What goes into the app zip (paths as stored in Git).
APP_PREFIXES = ("syncwatch/", "assets/", "tools/")
APP_FILES = ("CHANGELOG.md",)
NOT_FOR_FRIENDS = ("tools/release.py", "tools/release.bat", "tools/build_runtime.py", "tools/keywords.py",
                   "tools/bip39_english.txt")
ROOT_FILES = {"boot.py": "boot.py", "SyncWatch.bat": "SyncWatch.bat", "packaging/README.txt": "README.txt"}
EXTRA_FULL_FILES = {"packaging/THIRD_PARTY_NOTICES.txt": "THIRD_PARTY_NOTICES.txt"}


class Stop(Exception):
    """A problem the user has to fix; printed without a traceback."""


# =============================================================== small helpers
def say(text: str = "") -> None:
    print(text, flush=True)


def ask(prompt: str) -> str:
    return input(prompt).strip()


def run(command: List[str], check: bool = True, capture: bool = True, cwd: Path = ROOT) -> str:
    try:
        result = subprocess.run(command, cwd=cwd, capture_output=capture, text=True)
    except FileNotFoundError as exc:
        raise Stop(f"'{command[0]}' isn't installed or not on PATH ({exc}).") from exc
    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()[-1500:] if capture else ""
        raise Stop(f"'{' '.join(command[:4])}…' failed:\n{detail}")
    return (result.stdout or "") if capture else ""


def key_dir() -> Path:
    return paths.data_root() / "release"


def key_file() -> Path:
    return key_dir() / "signing_key.pem"


def pub_file() -> Path:
    return key_dir() / "signing_key.pub"


def read_trusted_keys(text: Optional[str] = None) -> List[str]:
    source = KEYS_FILE.read_text(encoding="utf-8") if text is None else text
    for node in ast.parse(source).body:
        if isinstance(node, ast.Assign) and any(getattr(t, "id", "") == "TRUSTED_KEYS" for t in node.targets):
            return [k for k in ast.literal_eval(node.value) if isinstance(k, str)]
    return []


def write_trusted_keys(keys: List[str]) -> None:
    text = KEYS_FILE.read_text(encoding="utf-8")
    body = "TRUSTED_KEYS = [\n" + "".join(f'    "{k}",\n' for k in keys) + "]"
    new_text, count = re.subn(r"TRUSTED_KEYS = \[.*?\]", body, text, count=1, flags=re.DOTALL)
    if count != 1:
        raise Stop("couldn't find TRUSTED_KEYS in syncwatch/update/keys.py")
    KEYS_FILE.write_text(new_text, encoding="utf-8")


def read_repo(text: Optional[str] = None) -> str:
    source = SOURCE_FILE.read_text(encoding="utf-8") if text is None else text
    match = re.search(r'^GITHUB_REPO = "([^"]*)"', source, re.MULTILINE)
    return match.group(1) if match else ""


def ask_passphrase(confirm: bool) -> bytes:
    while True:
        first = getpass.getpass("Passphrase (not shown while typing): ")
        if confirm and len(first) < MIN_PASSPHRASE:
            say(f"Use at least {MIN_PASSPHRASE} characters. A short sentence works well.")
            continue
        if confirm and getpass.getpass("Same passphrase again: ") != first:
            say("They didn't match, try again.")
            continue
        return first.encode("utf-8")


def save_key(private_key: Ed25519PrivateKey, passphrase: bytes) -> str:
    key_dir().mkdir(parents=True, exist_ok=True)
    pem = private_key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                    serialization.BestAvailableEncryption(passphrase))
    tmp = key_file().with_suffix(".tmp")
    tmp.write_bytes(pem)
    os.replace(tmp, key_file())
    public = manifest.public_key_text(private_key.public_key())
    pub_file().write_text(public + "\n", encoding="ascii")
    return public


def load_key(passphrase: bytes) -> Ed25519PrivateKey:
    if not key_file().exists():
        raise Stop("No signing key on this PC. Run 'release keygen' (first time) or 'release restore' "
                   "(you have your 24 words).")
    try:
        key = serialization.load_pem_private_key(key_file().read_bytes(), password=passphrase)
    except (ValueError, TypeError) as exc:
        raise Stop("Wrong passphrase (or the key file is damaged). If you forgot the passphrase, run "
                   "'release restore' with your 24 words and choose a new one.") from exc
    if not isinstance(key, Ed25519PrivateKey):
        raise Stop("the key file doesn't contain an Ed25519 key")
    return key


def type_words() -> bytes:
    say("Type your 24 words, separated by spaces. The first 4 letters of each word are enough.")
    while True:
        line = ask("> ")
        try:
            return keywords.from_words(line.split())
        except keywords.WordsError as exc:
            say(f"  {exc}")


# =============================================================== keygen / backup / restore
def cmd_keygen(args) -> None:
    if key_file().exists() and not args.replace:
        raise Stop(f"You already have a signing key ({key_file()}). Use 'release check-backup' to test "
                   "your paper copy. Replacing the key needs --replace, and friends' apps then need an "
                   "update that trusts the new key first. Read docs/RELEASING.md before doing that.")
    seed = os.urandom(32)
    private_key = Ed25519PrivateKey.from_private_bytes(seed)
    words = keywords.to_words(seed)
    say("\nYour signing key as 24 words. Write them on paper NOW, in order, and keep the paper safe.")
    say("Don't photograph them or store them on a computer, phone or cloud.\n")
    say(keywords.format_words(words))
    say("\nWhen you've written them down, press Enter.")
    ask("")
    say("\n" * 40 + "Check: type the words you wrote down at these positions.")
    for position in sorted(random.sample(range(24), 3)):
        for attempt in range(3):
            answer = ask(f"  Word {position + 1}: ").lower()
            if answer == words[position] or (len(answer) >= 4 and words[position].startswith(answer[:4])
                                             and answer in words[position]):
                break
            say("  That's not it. Check your paper.")
        else:
            raise Stop("The check didn't pass, so no key was saved. Run 'release keygen' again.")
    say("\nNow choose a passphrase. It locks the key file on this PC. If you forget it, your 24 words "
        "restore the key.")
    public = save_key(private_key, ask_passphrase(confirm=True))
    existing = read_trusted_keys()
    write_trusted_keys(existing + [public] if public not in existing else existing)
    say(f"\nSaved: {key_file()}")
    say(f"Your public key (safe to share): {public}")
    say("It was added to syncwatch/update/keys.py. Commit that change (see docs/GIT_GUIDE.md, lesson 3), "
        "then 'release check-backup' once to test your paper.")


def cmd_check_backup(args) -> None:
    seed = type_words()
    public = manifest.public_key_text(Ed25519PrivateKey.from_private_bytes(seed).public_key())
    trusted = read_trusted_keys()
    on_pc = pub_file().read_text(encoding="ascii").strip() if pub_file().exists() else ""
    if public in trusted or public == on_pc:
        say("Your 24 words are correct: they match " +
            ("the key in syncwatch/update/keys.py." if public in trusted else "the key file on this PC."))
    else:
        raise Stop("These words give a different key than yours. Check each word and its position.")


def cmd_restore(args) -> None:
    seed = type_words()
    private_key = Ed25519PrivateKey.from_private_bytes(seed)
    public = manifest.public_key_text(private_key.public_key())
    if public not in read_trusted_keys():
        say("Note: this key isn't in syncwatch/update/keys.py, so friends' apps won't accept releases "
            "signed with it.")
        if ask("Save it anyway? (y/n) ").lower() != "y":
            raise Stop("Nothing was saved.")
    if key_file().exists() and ask("A key file already exists. Replace it? (y/n) ").lower() != "y":
        raise Stop("Nothing was saved.")
    save_key(private_key, ask_passphrase(confirm=True))
    say(f"Restored: {key_file()}")


def cmd_show_words(args) -> None:
    private_key = load_key(ask_passphrase(confirm=False))
    seed = private_key.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
                                     serialization.NoEncryption())
    say("\nYour 24 words (write them on paper again, then close this window):\n")
    say(keywords.format_words(keywords.to_words(seed)))


def cmd_set_repo(args) -> None:
    repo = args.repo.strip()
    if not re.fullmatch(r"[A-Za-z0-9-]{1,39}/[A-Za-z0-9._-]{1,100}", repo):
        raise Stop("Write the repository as OWNER/NAME, for example alex-projects/syncwatch")
    text = SOURCE_FILE.read_text(encoding="utf-8")
    SOURCE_FILE.write_text(re.sub(r'^GITHUB_REPO = "[^"]*"', f'GITHUB_REPO = "{repo}"', text, flags=re.MULTILINE),
                           encoding="utf-8")
    say(f"syncwatch/update/source.py now says {repo}. Commit that change.")


# =============================================================== building
def git_files(ref: str = "HEAD") -> Dict[str, bytes]:
    """All files of a commit, as {path: bytes}. Only committed files can end up in a release."""
    data = subprocess.run(["git", "archive", "--format=tar", ref], cwd=ROOT, capture_output=True)
    if data.returncode != 0:
        raise Stop(f"git archive failed: {data.stderr.decode(errors='replace')[-500:]}")
    files = {}
    with tarfile.open(fileobj=io.BytesIO(data.stdout), mode="r:") as archive:
        for member in archive.getmembers():
            if member.isfile():
                files[member.name] = archive.extractfile(member).read()
    return files


def app_entries(files: Dict[str, bytes]) -> Dict[str, bytes]:
    """{path inside the app zip: bytes}"""
    entries = {}
    for name, data in files.items():
        if name in NOT_FOR_FRIENDS or "__pycache__" in name:
            continue
        if name.startswith(APP_PREFIXES) or name in APP_FILES:
            entries[name] = data
    for source, target in ROOT_FILES.items():
        if source in files:
            entries[f"_root/{target}"] = files[source]
    return entries


def write_zip(path: Path, entries: Dict[str, bytes]) -> manifest.FileRef:
    part = path.with_name(path.name + ".part")
    with zipfile.ZipFile(part, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name in sorted(entries):
            info = zipfile.ZipInfo(name, FIXED_DATE)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            archive.writestr(info, entries[name])
    os.replace(part, path)
    return manifest.FileRef(path.name, path.stat().st_size, manifest.sha256_file(path))


def write_full_zip(path: Path, app: Dict[str, bytes], files: Dict[str, bytes], runtime_zip: Path,
                   runtime_id: str) -> manifest.FileRef:
    say(f"  building {path.name} (everything in one zip, for new friends)…")
    part = path.with_name(path.name + ".part")
    started = time.monotonic()
    with zipfile.ZipFile(part, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as out, \
            zipfile.ZipFile(runtime_zip) as runtime:
        def put(name: str, data: bytes) -> None:
            info = zipfile.ZipInfo(f"SyncWatch/{name}", FIXED_DATE)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            out.writestr(info, data)

        for name in sorted(app):
            if name.startswith("_root/"):
                put(name[len("_root/"):], app[name])
            else:
                put(f"app/{name}", app[name])
        for source, target in EXTRA_FULL_FILES.items():
            if source in files:
                put(target, files[source])
        put("runtime/current.txt", runtime_id.encode("ascii"))
        put("movies/PUT_FILMS_HERE.txt", b"Put your films in this folder.\r\n")
        members = runtime.infolist()
        for index, member in enumerate(members, 1):
            if not member.is_dir():
                put(f"runtime/{runtime_id}/{member.filename}", runtime.read(member))
            if index % 500 == 0 or index == len(members):
                rate = index / max(0.001, time.monotonic() - started)
                print(f"\r  {index}/{len(members)} runtime files, about {(len(members) - index) / rate:3.0f} s left ",
                      end="", flush=True)
    print()
    os.replace(part, path)
    return manifest.FileRef(path.name, path.stat().st_size, manifest.sha256_file(path))


def changelog_notes(version: str, files: Dict[str, bytes]) -> str:
    text = files.get("CHANGELOG.md", b"").decode("utf-8")
    lines, taking = [], False
    for line in text.splitlines():
        if line.startswith("## "):
            if taking:
                break
            heading = line[3:].strip()
            taking = heading == version or heading.startswith(version + " ") or heading.startswith(version + "(")
            continue
        if taking:
            lines.append(line)
    notes = "\n".join(lines).strip()
    if not notes:
        raise Stop(f"CHANGELOG.md has no '## {version}' section. Add one (it becomes the release notes) and commit it.")
    return notes


# =============================================================== GitHub (gh)
def gh_json(args: List[str]):
    return json.loads(run(["gh"] + args) or "null")


def published_versions() -> List[Tuple[Version, str]]:
    releases = gh_json(["release", "list", "--limit", "100", "--json", "tagName"]) or []
    result = []
    for release in releases:
        try:
            result.append((Version(release["tagName"]), release["tagName"]))
        except (ValueError, KeyError):
            continue
    return sorted(result, reverse=True)


def find_published_runtime(runtime_id: str, repo: str) -> Optional[manifest.FileRef]:
    """A runtime zip already uploaded with an earlier release, as listed in that release's signed manifest."""
    name = f"SyncWatch-runtime-{runtime_id}.zip"
    for _version, tag in published_versions():
        assets = gh_json(["release", "view", tag, "--json", "assets"]).get("assets", [])
        if not any(a.get("name") == name for a in assets):
            continue
        with tempfile.TemporaryDirectory() as tmp:
            run(["gh", "release", "download", tag, "--pattern", "manifest.json", "--dir", tmp])
            data = Path(tmp, "manifest.json").read_bytes()
        published = manifest.parse(data, repo)
        if published.runtime and published.runtime.name == name:
            return manifest.FileRef(name, published.runtime.size, published.runtime.sha256,
                                    published.runtime.tag or tag)
    return None


# =============================================================== publish
def cmd_publish(args) -> None:
    say("== Checks ==")
    repo = read_repo()
    if not repo:
        raise Stop("No repository set. Run: release set-repo OWNER/NAME, and commit that change.")
    files = git_files()
    if read_repo(files.get("syncwatch/update/source.py", b"").decode()) != repo:
        raise Stop("syncwatch/update/source.py has changes that aren't committed. Commit them first.")
    trusted = read_trusted_keys(files.get("syncwatch/update/keys.py", b"").decode())
    if not trusted:
        raise Stop("No public key in the committed syncwatch/update/keys.py. Run 'release keygen' and commit.")
    version_text = re.search(r'__version__ = "([^"]+)"', files["syncwatch/__init__.py"].decode()).group(1)
    version = Version(version_text)
    if version.is_test != args.test:
        raise Stop(f"Version {version} {'is' if version.is_test else 'is not'} a test version, but you "
                   f"{'did' if args.test else 'did not'} use --test. Test versions look like 0.3.1-test.1.")
    if not args.dry_run:
        status = run(["git", "status", "--porcelain"]).strip()
        if status:
            raise Stop("You have changes that aren't committed:\n" + status +
                       "\nCommit them (or undo them) first, so the release matches what's on GitHub.")
        branch = run(["git", "rev-parse", "--abbrev-ref", "HEAD"]).strip()
        if branch != "main":
            raise Stop(f"You're on branch '{branch}'. Releases are made from main: git switch main")
        run(["git", "fetch", "origin", "--tags"])
        if run(["git", "rev-parse", "HEAD"]).strip() != run(["git", "rev-parse", "origin/main"]).strip():
            raise Stop("Your main isn't the same as GitHub's main. Run 'git pull' (or 'git push') first.")
        gh_repo = run(["gh", "repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner"]).strip()
        if gh_repo.lower() != repo.lower():
            raise Stop(f"This folder belongs to {gh_repo}, but source.py says {repo}.")
        if run(["git", "tag", "--list", f"v{version}"]).strip():
            raise Stop(f"Version {version} already has a tag. Raise the version in syncwatch/__init__.py.")
        published = published_versions()
        if published and published[0][0] >= version:
            raise Stop(f"{published[0][1]} is already published. The new version must be higher than that.")
    notes = changelog_notes(str(version), files)
    last_tag = run(["git", "describe", "--tags", "--abbrev=0"], check=False).strip()
    say(f"Repository: {repo}")
    say(f"Version:    {version} ({'test' if version.is_test else 'normal'} version)")
    if last_tag:
        log = run(["git", "log", "--oneline", f"{last_tag}..HEAD"], check=False).strip()
        stat = run(["git", "diff", "--shortstat", f"{last_tag}..HEAD"], check=False).strip()
        say(f"Changes since {last_tag}: {stat or 'none'}\n{log}")
    say("\nRelease notes (from CHANGELOG.md):\n" + notes + "\n")

    if not args.skip_tests:
        say("== Automatic tests ==")
        env = dict(os.environ, QT_QPA_PLATFORM="offscreen")
        result = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "tests", "-p", "test_*.py"],
                                cwd=ROOT, env=env)
        if result.returncode != 0:
            raise Stop("Tests failed, so nothing was published. Fix them first (or ask Claude).")

    say("== Signing key ==")
    private_key = load_key(ask_passphrase(confirm=False))
    public = manifest.public_key_text(private_key.public_key())
    if public not in trusted:
        raise Stop("Your signing key isn't in the committed syncwatch/update/keys.py, so friends' apps would "
                   "refuse this release.")

    say("== Building ==")
    out = ROOT / "dist" / f"v{version}"
    shutil.rmtree(out, ignore_errors=True)
    out.mkdir(parents=True)
    app = app_entries(files)
    app_ref = write_zip(out / f"SyncWatch-{version}-app.zip", app)
    say(f"  {app_ref.name}: {app_ref.size / 1e6:.1f} MB")
    runtime_id = build_runtime.runtime_id()
    runtime_ref = None if args.dry_run else find_published_runtime(runtime_id, repo)
    runtime_zip: Optional[Path] = None
    upload_runtime = runtime_ref is None
    if runtime_ref is not None:
        say(f"  runtime {runtime_id}: reusing the one published with {runtime_ref.tag}")
    else:
        runtime_zip = build_runtime.build(ROOT / "dist")
        runtime_ref = manifest.FileRef(runtime_zip.name, runtime_zip.stat().st_size,
                                       manifest.sha256_file(runtime_zip))
    full_zip = args.full_zip if args.full_zip is not None else not version.is_test
    full_ref = None
    if full_zip:
        if runtime_zip is None:
            cached = ROOT / "dist" / runtime_ref.name
            if not cached.exists() or manifest.sha256_file(cached) != runtime_ref.sha256:
                say("  downloading the published runtime for the all-in-one zip…")
                run(["gh", "release", "download", runtime_ref.tag, "--pattern", runtime_ref.name,
                     "--dir", str(ROOT / "dist"), "--clobber"], capture=False)
            if manifest.sha256_file(cached) != runtime_ref.sha256:
                raise Stop("the downloaded runtime doesn't match its published fingerprint")
            runtime_zip = cached
        full_ref = write_full_zip(out / f"SyncWatch-{version}-windows.zip", app, files, runtime_zip, runtime_id)
        say(f"  {full_ref.name}: {full_ref.size / 1e6:.0f} MB")
    released = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    manifest_bytes = manifest.build(str(version), repo, notes, runtime_id, app_ref, runtime_ref, full_ref, released)
    (out / "manifest.json").write_bytes(manifest_bytes)
    (out / "manifest.json.sig").write_bytes(manifest.sign(manifest_bytes, private_key))
    manifest.verify_signature(manifest_bytes, (out / "manifest.json.sig").read_bytes(), trusted)
    (out / "notes.md").write_text(notes + "\n", encoding="utf-8")
    uploads = [out / "manifest.json", out / "manifest.json.sig", out / app_ref.name]
    if upload_runtime and runtime_zip is not None:
        uploads.append(runtime_zip)
    if full_ref is not None:
        uploads.append(out / full_ref.name)
    say("  signed manifest.json")
    if args.dry_run:
        say(f"\nDry run: everything is in {out}. Nothing was published.")
        return

    say("\n== Publish ==")
    if ask(f"Type {version} to publish it to everyone{' who gets test versions' if version.is_test else ''}: ") \
            != str(version):
        raise Stop("Not published.")
    run(["git", "tag", "-a", f"v{version}", "-m", f"SyncWatch {version}"])
    run(["git", "push", "origin", f"v{version}"])
    command = ["gh", "release", "create", f"v{version}", "--verify-tag", "--title", f"SyncWatch {version}",
               "--notes-file", str(out / "notes.md")]
    if version.is_test:
        command += ["--prerelease", "--latest=false"]
    say("  uploading (this can take a while for the big zips)…")
    run(command + [str(p) for p in uploads], capture=False)

    say("== Checking what GitHub now has ==")
    with tempfile.TemporaryDirectory() as tmp:
        run(["gh", "release", "download", f"v{version}", "--pattern", "manifest.json*", "--dir", tmp])
        online = Path(tmp, "manifest.json").read_bytes()
        manifest.verify_signature(online, Path(tmp, "manifest.json.sig").read_bytes(), trusted)
        if online != manifest_bytes:
            raise Stop("GitHub's manifest.json differs from the one just built. Don't share this release; ask Claude.")
    say(f"\nPublished SyncWatch {version}. Friends' apps will offer it within a few hours, or right away "
        "with Settings > Updates > Check now.")


# =============================================================== entry
def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="release", description="Publish SyncWatch and manage the signing key.")
    sub = parser.add_subparsers(dest="command", required=True)
    keygen = sub.add_parser("keygen", help="create your signing key (once)")
    keygen.add_argument("--replace", action="store_true", help="make a new key even though one exists")
    sub.add_parser("check-backup", help="check your 24 words")
    sub.add_parser("restore", help="rebuild the key file from your 24 words")
    sub.add_parser("show-words", help="show your 24 words again (needs the passphrase)")
    set_repo = sub.add_parser("set-repo", help="set the GitHub repository (OWNER/NAME)")
    set_repo.add_argument("repo")
    publish = sub.add_parser("publish", help="publish the current version")
    publish.add_argument("--test", action="store_true")
    publish.add_argument("--dry-run", action="store_true")
    publish.add_argument("--skip-tests", action="store_true")
    publish.add_argument("--full-zip", dest="full_zip", action="store_true", default=None)
    publish.add_argument("--no-full-zip", dest="full_zip", action="store_false")
    args = parser.parse_args(argv)
    handlers = {"keygen": cmd_keygen, "check-backup": cmd_check_backup, "restore": cmd_restore,
                "show-words": cmd_show_words, "set-repo": cmd_set_repo, "publish": cmd_publish}
    try:
        handlers[args.command](args)
    except Stop as stop:
        say(f"\nStopped: {stop}")
        return 1
    except KeyboardInterrupt:
        say("\nCancelled.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
