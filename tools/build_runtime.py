"""Builds SyncWatch's portable Windows runtime: its own Python plus libraries, in one zip.

    python tools/build_runtime.py [--out FOLDER]

Steps (each checked; downloads are cached, so running it again doesn't download again):
  1. Download the official Windows "embeddable" Python from python.org and check
     its SHA-256 against runtime-spec/runtime.json.
  2. Install the exact library files from runtime-spec/requirements-win.lock
     (pip checks each file's SHA-256).
  3. Tell that Python where the libraries are (the python3XX._pth file).
  4. On Windows: start it once and import the video library and cryptography.
  5. Zip it as SyncWatch-runtime-<id>.zip with a fixed file order and dates,
     so the same spec always gives the same zip.

The id comes from the spec files: same spec, same id. tools/release.py only
builds and uploads a runtime when the id is new, which is rare.
Works from any OS (pip can fetch Windows files anywhere), but step 4 only runs
on Windows.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
import zipfile
from pathlib import Path

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


def download(url: str, target: Path, expected_sha256: str) -> Path:
    if target.exists() and sha256(target) == expected_sha256:
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
    if actual != expected_sha256:
        part.unlink()
        raise BuildError(f"{target.name}: SHA-256 is {actual}, expected {expected_sha256}. "
                         "Nothing was built. Check runtime-spec/runtime.json.")
    os.replace(part, target)
    return target


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

    print("1/5 Official Windows embeddable Python")
    embed = download(info["embed_url"], cache_dir() / Path(info["embed_url"]).name, info["embed_sha256"])
    with zipfile.ZipFile(embed) as archive:
        archive.extractall(work)

    print("2/5 Libraries (each file checked against requirements-win.lock)")
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

    print("3/5 Library path (python._pth)")
    pth = work / f"python{tag}._pth"
    if not pth.exists():
        raise BuildError(f"{pth.name} not found in the embeddable Python")
    pth.write_text(f"python{tag}.zip\n.\nLib\\site-packages\nimport site\n", encoding="ascii")
    (work / "RUNTIME_ID").write_text(rid, encoding="ascii")
    if not any(site_packages.rglob("msvcp140.dll")):
        print("  note: msvcp140.dll isn't bundled; SyncWatch then relies on the Microsoft "
              "Visual C++ runtime installed on the PC (almost always present).")

    print("4/5 Test start")
    if sys.platform == "win32":
        check = subprocess.run([str(work / "python.exe"), "-c",
                                "import PySide6.QtMultimedia, PySide6.QtMultimediaWidgets, cryptography; "
                                "print('runtime ok')"], capture_output=True, text=True, timeout=180)
        print("  " + (check.stdout.strip() or check.stderr.strip()[-500:]))
        if check.returncode != 0:
            raise BuildError("the new runtime didn't start (see above). Nothing was zipped.")
    else:
        print("  skipped (only possible on Windows)")

    print("5/5 Zip")
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
