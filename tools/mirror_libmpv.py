"""Keeps a copy of the libmpv build SyncWatch uses in a pre-release of this GitHub repo.

    python tools/mirror_libmpv.py [--upload]
    python tools/mirror_libmpv.py --from <URL of a newer mpv-dev-x86_64-*.7z> <its SHA-256>

Why: mpv's Windows builds come from projects that delete old builds after a few weeks
(zhongfly/mpv-winbuild), and every SyncWatch release must be rebuildable with exactly the
file that was tested. So this tool

1. downloads the build named in runtime-spec/runtime.json ("libmpv": upstream_url) and
   checks its SHA-256,
2. takes libmpv-2.dll out of it (with 7-Zip: these archives use a 7z filter Python can't
   read, so 7z must be installed: it is on GitHub's machines) and checks the DLL's SHA-256,
3. packs it as a .zip that Python opens by itself, with SOURCE.txt saying where it came
   from and where its source code is (mpv is GPL software),
4. with --upload: puts that zip into the pre-release named in "mirror_url", with gh. The
   updater never looks at it: it only uses releases that contain the signed manifest.json.

You don't need to run it on your PC: on GitHub, Actions > "Mirror libmpv" > Run workflow
does exactly this (.github/workflows/mirror-libmpv.yml).

Updating mpv later: run with --from and a newer build. It prints a new "libmpv" section
for runtime.json; paste it, commit, run the workflow, test, release.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build_runtime  # noqa: E402

FIXED_DATE = (2020, 1, 1, 0, 0, 0)
NAME = re.compile(r"mpv-dev-x86_64-(\d{8})-git-([0-9a-f]{7,12})\.7z$")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--from", dest="source", nargs=2, metavar=("URL", "SHA256"),
                        help="use a different (newer) build than the one in runtime.json")
    parser.add_argument("--upload", action="store_true", help="create the pre-release on GitHub (needs gh)")
    parser.add_argument("--out", default="dist", help="where to put the zip (default: dist)")
    args = parser.parse_args()
    spec = dict(build_runtime.spec()["libmpv"])
    if args.source:
        spec["upstream_url"], spec["upstream_sha256"] = args.source[0], args.source[1].lower()
    match = NAME.search(spec["upstream_url"])
    if not match:
        print("The build's URL should end in mpv-dev-x86_64-<date>-git-<commit>.7z")
        return 1
    date, commit = match.groups()
    try:
        data = build_runtime._libmpv_from_upstream(spec)
    except build_runtime.BuildError as exc:
        print(f"ERROR: {exc}")
        return 1
    dll_sha = hashlib.sha256(data).hexdigest()
    if not args.source and dll_sha != spec["dll_sha256"]:
        print(f"ERROR: libmpv-2.dll has SHA-256 {dll_sha}, but runtime.json says {spec['dll_sha256']}.")
        return 1
    found = re.search(rb"mpv v?\d+\.\d+\.\d+[-\w.]*", data)
    version = found.group(0).decode("ascii", "replace") if found else f"mpv git {commit}"
    tag = f"deps-libmpv-{date}-{commit}"
    source_note = f"""libmpv-2.dll: the video player library inside SyncWatch (from version 0.4.2)

Version: {version}
Built by: zhongfly/mpv-winbuild (https://github.com/zhongfly/mpv-winbuild), x86_64
Original download: {spec["upstream_url"]}
  SHA-256 of that file: {spec["upstream_sha256"]}
SHA-256 of libmpv-2.dll: {dll_sha}

mpv and the libraries built into this DLL (FFmpeg and others) are free software. This
build is distributed under the GNU General Public License: it includes GPL-licensed parts.
Source code of mpv at this version: https://github.com/mpv-player/mpv/tree/{commit}
How it was built, and the exact version of every library inside:
https://github.com/zhongfly/mpv-winbuild (the build from {date[:4]}-{date[4:6]}-{date[6:]}, commit {commit}).
"""
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    target = out / f"libmpv-2-x86_64-{date}-{commit}.zip"
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as packed:
        for name, content in (("libmpv-2.dll", data), ("SOURCE.txt", source_note.encode("utf-8"))):
            entry = zipfile.ZipInfo(name, FIXED_DATE)
            entry.compress_type = zipfile.ZIP_DEFLATED
            entry.external_attr = 0o644 << 16
            packed.writestr(entry, content)
    print(f"made {target} ({target.stat().st_size / 1e6:.1f} MB)")
    repo = re.search(r"github\.com/([^/]+/[^/]+)/releases", spec.get("mirror_url", ""))
    repo_name = repo.group(1) if repo else "<owner>/<repo>"
    if args.upload:
        notes = ("The video player library SyncWatch uses, kept here so every release can be rebuilt with exactly "
                 "this file. Not a SyncWatch version: the updater ignores it (it only uses releases with a signed "
                 "manifest.json).\n\n" + source_note + "\nMade by tools/mirror_libmpv.py.")
        subprocess.run(["gh", "release", "create", tag, str(target), "--repo", repo_name, "--prerelease",
                        "--title", f"libmpv {version} for SyncWatch (not a SyncWatch version)", "--notes", notes],
                       check=True)
        print(f"uploaded as the pre-release {tag}")
    section = {"libmpv": {
        "_help": spec.get("_help", ""),
        "version": version,
        "dll_sha256": dll_sha,
        "mirror_url": f"https://github.com/{repo_name}/releases/download/{tag}/{target.name}",
        "upstream_url": spec["upstream_url"],
        "upstream_sha256": spec["upstream_sha256"],
    }}
    print("\nThe \"libmpv\" section for runtime-spec/runtime.json:\n" + json.dumps(section, indent=2)[1:-1].strip())
    return 0


if __name__ == "__main__":
    sys.exit(main())
