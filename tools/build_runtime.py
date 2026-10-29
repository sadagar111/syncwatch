"""Builds SyncWatch's portable Windows runtime: its own Python plus libraries, in one zip.

    python tools/build_runtime.py [--out FOLDER]

Steps (each checked; downloads are cached, so running it again doesn't download again):
  1. Download the official Windows "embeddable" Python from python.org and check
     its SHA-256 against runtime-spec/runtime.json.
  2. Install the exact library files from runtime-spec/requirements-win.lock
     (pip checks each file's SHA-256).
  3. The video player library, libmpv-2.dll (mpv), into runtime/libmpv: from the
     copy in this repo's "deps-libmpv-…" pre-release, or else from the original
     build (needs 7-Zip). Either way the DLL must match "dll_sha256" in runtime.json.
  4. Tell that Python where the libraries are (the python3XX._pth file).
  5. Remove what SyncWatch never uses (the "remove" list in runtime.json) and
     check that no path is longer than "max_path".
  6. On Windows: start it once: Qt's video part, cryptography, the converter (PyAV),
     and mpv (loaded and started without a picture).
  7. Zip it as SyncWatch-runtime-<id>.zip with a fixed file order and dates,
     so the same spec always gives the same zip.

The id comes from the spec files: same spec, same id. tools/release.py only
builds and uploads a runtime when the id is new, which is rare.
Works from any OS (pip can fetch Windows files anywhere), but step 4 only runs
on Windows.
"""
from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
import zipfile
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent
SPEC = ROOT / "runtime-spec"
FIXED_DATE = (2020, 1, 1, 0, 0, 0)


class BuildError(Exception):
    pass


def spec() -> dict:
    return json.loads((SPEC / "runtime.json").read_text(encoding="utf-8"))


def runtime_id() -> str:
    digest = hashlib.sha256()
    for name in ("runtime.json", "requirements-win.lock"):
        digest.update((SPEC / name).read_bytes().replace(b"\r\n", b"\n"))
    return "rt-" + digest.hexdigest()[:10]


def prune(work: Path, patterns: list) -> tuple:
    """Delete the files matching any pattern (matched against the path inside the runtime)."""
    removed = saved = 0
    for path in sorted(p for p in work.rglob("*") if p.is_file()):
        relative = path.relative_to(work).as_posix()
        if any(fnmatch.fnmatchcase(relative, pattern) for pattern in patterns):
            saved += path.stat().st_size
            path.unlink()
            removed += 1
    for folder in sorted((p for p in work.rglob("*") if p.is_dir()), key=lambda p: -len(p.parts)):
        if not any(folder.iterdir()):
            folder.rmdir()  # leave no empty folders behind
    return removed, saved


def check_path_lengths(work: Path, limit: int) -> None:
    longest = max((p.relative_to(work).as_posix() for p in work.rglob("*") if p.is_file()), key=len)
    print(f"  longest path: {len(longest)} characters (limit {limit})")
    if len(longest) > limit:
        raise BuildError(f"'{longest}' is {len(longest)} characters long, over the limit of {limit}. Windows "
                         "can't unzip such long paths into most folders. Add it to \"remove\" in runtime.json "
                         "if SyncWatch doesn't need it.")


def cache_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / ".cache")
    folder = Path(base) / "SyncWatch" / "build-cache"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def download(url: str, target: Path, expected_sha256: Optional[str]) -> Path:
    """Download (with progress) into the cache. ``expected_sha256`` None: the caller checks the content."""
    if target.exists() and (expected_sha256 is None or sha256(target) == expected_sha256):
        print(f"  using cached {target.name}")
        return target
    print(f"  downloading {url}")
    part = target.with_name(target.name + ".part")
    started = time.monotonic()
    with urllib.request.urlopen(url, timeout=60) as response, open(part, "wb") as out:
        total = int(response.headers.get("Content-Length", 0) or 0)
        done = 0
        while True:
            block = response.read(1 << 18)
            if not block:
                break
            out.write(block)
            done += len(block)
            speed = done / max(0.001, time.monotonic() - started)
            if total:
                left = (total - done) / speed if speed else 0
                print(f"\r  {done / 1e6:5.1f} / {total / 1e6:.1f} MB, about {left:3.0f} s left ", end="", flush=True)
    print()
    actual = sha256(part)
    if expected_sha256 is not None and actual != expected_sha256:
        part.unlink()
        raise BuildError(f"{target.name}: SHA-256 is {actual}, expected {expected_sha256}. "
                         "Nothing was built. Check runtime-spec/runtime.json.")
    os.replace(part, target)
    return target


def libmpv_dll(info: dict, folder: Path) -> Path:
    """Put the pinned libmpv-2.dll into ``folder``. Its SHA-256 must be "dll_sha256" in runtime.json.

    First choice: the zip in this repo's pre-release (made by tools/mirror_libmpv.py; Python
    opens it by itself). If that isn't there (yet), the original build, which needs 7-Zip.
    """
    spec = info["libmpv"]
    wanted = spec["dll_sha256"]
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / "libmpv-2.dll"
    if target.exists() and sha256(target) == wanted:
        print(f"  {target} is already the right one")
        return target
    mirror = cache_dir() / Path(spec["mirror_url"]).name
    try:
        download(spec["mirror_url"], mirror, None)
        with zipfile.ZipFile(mirror) as packed:
            data = packed.read("libmpv-2.dll")
        source = "the copy in the deps-libmpv pre-release"
    except (OSError, KeyError, zipfile.BadZipFile) as exc:
        mirror.unlink(missing_ok=True)
        print(f"  the copy in the deps-libmpv pre-release isn't available ({exc}); trying the original build")
        data = _libmpv_from_upstream(spec)
        source = "the original build"
    actual = hashlib.sha256(data).hexdigest()
    if actual != wanted:
        mirror.unlink(missing_ok=True)
        raise BuildError(f"libmpv-2.dll from {source} has SHA-256 {actual}, expected {wanted}. Nothing was built.")
    part = target.with_name(target.name + ".part")
    part.write_bytes(data)
    os.replace(part, target)
    (folder / "SOURCE.txt").write_text(libmpv_source_note(spec), encoding="utf-8")
    print(f"  libmpv-2.dll ({len(data) / 1e6:.0f} MB, {spec.get('version', '?')}) from {source}, SHA-256 checked")
    return target


def libmpv_source_note(spec: dict) -> str:
    """SOURCE.txt next to the DLL: what it is and where its source code is (mpv is GPL software)."""
    commit = spec["upstream_url"].rstrip("/").split("-git-")[-1].split(".")[0]
    return (f"libmpv-2.dll: the video player library inside SyncWatch (from version 0.4.2)\n\n"
            f"Version: {spec.get('version', '?')}\n"
            f"Built by zhongfly/mpv-winbuild (https://github.com/zhongfly/mpv-winbuild), x86_64.\n"
            f"Original download: {spec['upstream_url']}\n"
            f"SHA-256 of libmpv-2.dll: {spec['dll_sha256']}\n\n"
            "mpv and the libraries built into this DLL (FFmpeg and others) are free software. This build is\n"
            "distributed under the GNU General Public License (it includes GPL-licensed parts).\n"
            f"Source code of mpv at this version: https://github.com/mpv-player/mpv/tree/{commit}\n"
            "How it was built, and the exact version of every library inside: https://github.com/zhongfly/mpv-winbuild\n")


def _libmpv_from_upstream(spec: dict) -> bytes:
    seven_zip = shutil.which("7z") or shutil.which("7z.exe") or shutil.which("7za")
    if seven_zip is None and sys.platform == "win32":
        candidate = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "7-Zip" / "7z.exe"
        seven_zip = str(candidate) if candidate.is_file() else None
    if seven_zip is None:
        raise BuildError("the libmpv copy isn't in the deps-libmpv pre-release yet, and 7-Zip (needed to unpack the "
                         "original build) isn't installed. Run the \"Mirror libmpv\" workflow on GitHub (Actions tab) "
                         "once, or install 7-Zip from https://www.7-zip.org/.")
    archive = download(spec["upstream_url"], cache_dir() / Path(spec["upstream_url"]).name, spec["upstream_sha256"])
    with tempfile.TemporaryDirectory() as tmp:
        result = subprocess.run([seven_zip, "x", "-y", f"-o{tmp}", str(archive), "libmpv-2.dll"],
                                capture_output=True, text=True)
        dll = Path(tmp) / "libmpv-2.dll"
        if result.returncode != 0 or not dll.is_file():
            raise BuildError(f"7-Zip couldn't unpack {archive.name}: {(result.stderr or result.stdout)[-500:]}")
        return dll.read_bytes()


def build(out_dir: Path) -> Path:
    info = spec()
    rid = runtime_id()
    out_dir.mkdir(parents=True, exist_ok=True)
    final_zip = out_dir / f"SyncWatch-runtime-{rid}.zip"
    if final_zip.exists():
        print(f"Runtime {rid} is already built: {final_zip}")
        return final_zip
    print(f"Building runtime {rid} (Python {info['python_version']})")
    work = out_dir / f"runtime-build-{rid}"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)

    print("1/7 Official Windows embeddable Python")
    embed = download(info["embed_url"], cache_dir() / Path(info["embed_url"]).name, info["embed_sha256"])
    with zipfile.ZipFile(embed) as archive:
        archive.extractall(work)

    print("2/7 Libraries (each file checked against requirements-win.lock)")
    site_packages = work / "Lib" / "site-packages"
    site_packages.mkdir(parents=True)
    tag = info["python_tag"]
    command = [sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "--no-compile",
               "--target", str(site_packages), "--platform", "win_amd64",
               "--python-version", f"{tag[0]}.{tag[1:]}", "--implementation", "cp",
               "--abi", f"cp{tag}", "--abi", "abi3", "--abi", "none",
               "--only-binary=:all:", "--no-deps", "--require-hashes",
               "-r", str(SPEC / "requirements-win.lock")]
    result = subprocess.run(command)
    if result.returncode != 0:
        raise BuildError("pip couldn't install the pinned libraries (see the messages above).")

    print("3/7 Video player library (libmpv)")
    libmpv_dll(info, work / "libmpv")

    print("4/7 Library path (python._pth)")
    pth = work / f"python{tag}._pth"
    if not pth.exists():
        raise BuildError(f"{pth.name} not found in the embeddable Python")
    pth.write_text(f"python{tag}.zip\n.\nLib\\site-packages\nimport site\n", encoding="ascii")
    (work / "RUNTIME_ID").write_text(rid, encoding="ascii")
    if not any(site_packages.rglob("msvcp140.dll")):
        print("  note: msvcp140.dll isn't bundled; SyncWatch then relies on the Microsoft "
              "Visual C++ runtime installed on the PC (almost always present).")

    print("5/7 Remove what SyncWatch doesn't use")
    removed, saved = prune(work, info.get("remove", []))
    print(f"  {removed} files, {saved / 1e6:.0f} MB")
    check_path_lengths(work, int(info.get("max_path", 100)))

    print("6/7 Test start")
    if sys.platform == "win32":
        check = subprocess.run([str(work / "python.exe"), "-c",
                                "import sys; sys.path.insert(0, sys.argv[1]); "
                                "import PySide6.QtMultimedia, PySide6.QtMultimediaWidgets, cryptography, av; "
                                "from syncwatch import mpvlib; player = mpvlib.load().MPV(vo='null', ao='null'); "
                                "print('runtime ok:', player.mpv_version, '| PyAV', av.__version__); "
                                "player.terminate()", str(ROOT)], capture_output=True, text=True, timeout=180)
        print("  " + (check.stdout.strip() or check.stderr.strip()[-500:]))
        if check.returncode != 0:
            raise BuildError("the new runtime didn't start (see above). Nothing was zipped.")
    else:
        print("  skipped (only possible on Windows)")

    print("7/7 Zip")
    files = sorted(p for p in work.rglob("*") if p.is_file())
    part = final_zip.with_name(final_zip.name + ".part")
    started = time.monotonic()
    with zipfile.ZipFile(part, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for index, path in enumerate(files, 1):
            info_entry = zipfile.ZipInfo(path.relative_to(work).as_posix(), FIXED_DATE)
            info_entry.compress_type = zipfile.ZIP_DEFLATED
            info_entry.external_attr = 0o644 << 16
            archive.writestr(info_entry, path.read_bytes())
            if index % 200 == 0 or index == len(files):
                rate = index / max(0.001, time.monotonic() - started)
                print(f"\r  {index}/{len(files)} files, about {(len(files) - index) / rate:3.0f} s left ",
                      end="", flush=True)
    print()
    os.replace(part, final_zip)
    shutil.rmtree(work, ignore_errors=True)
    print(f"Done: {final_zip} ({final_zip.stat().st_size / 1e6:.0f} MB)")
    return final_zip


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", default=str(ROOT / "dist"), help="where to put the zip (default: dist)")
    parser.add_argument("--print-id", action="store_true", help="only print the runtime id")
    args = parser.parse_args()
    if args.print_id:
        print(runtime_id())
        return 0
    try:
        build(Path(args.out))
    except BuildError as exc:
        print(f"\nERROR: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
