"""Brings SyncWatch's YouTube parts up to date before a release: yt-dlp, yt-dlp-ejs and Deno.

    python tools/update_youtube_parts.py          show what's new (changes nothing)
    python tools/update_youtube_parts.py --write  write the new versions and fingerprints

YouTube changes its pages every few weeks; then yt-dlp needs a new version (SW-605 for friends).
Friends only get it through a SyncWatch release that you sign, so nothing reaches their PCs that
you didn't approve. This script does the boring part: it asks PyPI for the newest yt-dlp, the
exact yt-dlp-ejs version that yt-dlp asks for, and the newest Deno, and puts their versions and
SHA-256 fingerprints (from PyPI, checked again when the runtime is built) into
runtime-spec/requirements-win.lock and requirements.txt. Then: run the tests, make a release.

Nothing is downloaded except PyPI's small JSON descriptions; it checks first and changes nothing
if everything is already current.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.request
from pathlib import Path
from typing import Dict, Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent
LOCK = ROOT / "runtime-spec" / "requirements-win.lock"
REQUIREMENTS = ROOT / "requirements.txt"
PARTS = ("yt-dlp", "yt-dlp-ejs", "deno")


def pypi(package: str, version: str = "") -> Dict:
    url = f"https://pypi.org/pypi/{package}/{version + '/' if version else ''}json"
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "SyncWatch"}), timeout=30) as answer:
        return json.loads(answer.read().decode("utf-8"))


def windows_file(release: Dict) -> Tuple[str, str]:
    """(file name, sha256) of the wheel the Windows runtime can use (pure Python or win_amd64)."""
    for entry in release["urls"]:
        name = entry["filename"]
        if entry["packagetype"] == "bdist_wheel" and (name.endswith("-none-any.whl") or "win_amd64" in name):
            return name, entry["digests"]["sha256"]
    raise LookupError(f"no wheel for Windows in {release['info']['name']} {release['info']['version']}")


def ejs_version(ytdlp: Dict) -> Optional[str]:
    """yt-dlp pins the exact yt-dlp-ejs it works with (in its 'default' extra)."""
    for requirement in ytdlp["info"].get("requires_dist") or []:
        match = re.match(r"yt-dlp-ejs\s*==\s*([\w.]+)", requirement)
        if match:
            return match.group(1)
    return None


def pinned(text: str, package: str) -> Optional[str]:
    match = re.search(rf"^{re.escape(package)}==([\w.]+)", text, re.MULTILINE)
    return match.group(1) if match else None


def replace_pin(text: str, package: str, version: str, sha256: str) -> str:
    pattern = re.compile(rf"^{re.escape(package)}==[\w.]+ \\\n    --hash=sha256:[0-9a-f]{{64}}", re.MULTILINE)
    new = f"{package}=={version} \\\n    --hash=sha256:{sha256}"
    if not pattern.search(text):
        raise LookupError(f"{package} isn't pinned in {LOCK.name}")
    return pattern.sub(new, text, count=1)


def main(argv) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--write", action="store_true", help="write the new versions (default: only show them)")
    args = parser.parse_args(argv)
    lock = LOCK.read_text(encoding="utf-8")
    ytdlp = pypi("yt-dlp")
    wanted = {"yt-dlp": ytdlp["info"]["version"], "deno": pypi("deno")["info"]["version"]}
    ejs = ejs_version(ytdlp)
    if not ejs:
        print("Couldn't tell which yt-dlp-ejs this yt-dlp wants; nothing changed.")
        return 1
    wanted["yt-dlp-ejs"] = ejs
    changes = {name: (pinned(lock, name), version) for name, version in wanted.items()
               if pinned(lock, name) != version}
    for name in PARTS:
        now = pinned(lock, name)
        print(f"  {name:11s} {now or '-':>12s}  ->  {wanted[name]}" + ("" if name in changes else "  (current)"))
    if not changes:
        print("Everything is current. Nothing to do.")
        return 0
    if not args.write:
        print("Run again with --write to put the new versions in (then run the tests and release).")
        return 0
    for name, (_old, version) in changes.items():
        _file, sha256 = windows_file(pypi(name, version))
        lock = replace_pin(lock, name, version, sha256)
    LOCK.write_text(lock, encoding="utf-8")
    requirements = REQUIREMENTS.read_text(encoding="utf-8")
    for name in ("yt-dlp", "deno"):
        if name in changes:
            requirements = re.sub(rf"^({re.escape(name)}(?:\[default\])?)>=[\w.]+", rf"\g<1>>={wanted[name]}",
                                  requirements, count=1, flags=re.MULTILINE)
    REQUIREMENTS.write_text(requirements, encoding="utf-8")
    print(f"Written: {LOCK.relative_to(ROOT)} and {REQUIREMENTS.name}. Next: run the tests, then make a release "
          "(the runtime gets a new id, so friends download the new parts once).")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
