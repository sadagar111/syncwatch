"""The real-app checks (offscreen): the whole window, the film library, three apps in one room,
the update cycle.

    python tests/gui_checks.py [ui|library|room|update|all] [--out FOLDER]

Exit code 0 only if every chosen check passes. Screenshots and logs go to
--out (GitHub uploads that folder, so you can look at the screenshots of each
automatic test run). Used by .github/workflows/tests.yml; runs anywhere with
PySide6 installed.
"""
import argparse
import bisect
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
FILM = HERE / "media" / "demo.mkv"
ENV = dict(os.environ, QT_QPA_PLATFORM="offscreen")
if sys.platform.startswith("linux"):
    ENV.setdefault("QT_AUDIO_BACKEND", "pulseaudio")  # avoids a PipeWire crash without a sound server


def check_ui(out: Path) -> bool:
    result = subprocess.run([sys.executable, str(HERE / "ui_harness.py"), "normal", str(out / "ui"), str(FILM)],
                            env=ENV, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300)
    (out / "ui.log").write_text(result.stdout + "\n" + result.stderr, encoding="utf-8")
    print("\n".join(line for line in result.stdout.splitlines() if line.startswith(("KEY CHECKS", "UI HARNESS",
                                                                                     "ROOM CHECKS", "PLAYER",
                                                                                     "subtitle sources"))))
    player = next((line[len("PLAYER:"):].strip() for line in result.stdout.splitlines()
                   if line.startswith("PLAYER:")), "")
    if player and os.environ.get("GITHUB_ACTIONS"):   # shown on the pull request page
        print(f"::notice title=Video player in the app check ({sys.platform})::{player}", flush=True)
    return result.returncode == 0


def check_library(out: Path) -> bool:
    """Add film (check, copy, open), a duplicate, a film whose sound gets converted, removing."""
    result = subprocess.run([sys.executable, str(HERE / "library_harness.py"), str(out / "library")], env=ENV,
                            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=600)
    (out / "library.log").write_text(result.stdout + "\n" + result.stderr, encoding="utf-8")
    print("\n".join(line for line in result.stdout.splitlines()
                    if line.startswith(("LIBRARY", "steps seen", "converted copy", "library now", "ERRORS SEEN"))))
    if result.returncode != 0:
        print("library check failed; end of its output:")
        print("\n".join((result.stdout + "\n" + result.stderr).strip().splitlines()[-40:]))
    return result.returncode == 0


def _load(path: Path):
    samples, events, people = [], [], []
    for line in path.read_text().splitlines():
        entry = json.loads(line)
        if "pos" in entry:
            samples.append((entry["t"], entry["pos"]))
            people.append((entry["t"], entry.get("people", 0)))
        elif "event" in entry and "t" in entry:
            events.append((entry["t"], entry["event"]))
    return samples, events, people


def _position(samples, t):
    times = [s[0] for s in samples]
    i = bisect.bisect_left(times, t)
    if i <= 0 or i >= len(samples):
        return None
    (t0, p0), (t1, p1) = samples[i - 1], samples[i]
    return p0 + (p1 - p0) * (t - t0) / (t1 - t0) if t1 > t0 else p0


def check_room(out: Path) -> bool:
    """Three real apps (host + two guests) with real video players in one room, through a relay on
    this PC, driven by tests/room_gui_harness.py."""
    sys.path.insert(0, str(ROOT))
    from tools.local_relay import LocalRelay
    relay = LocalRelay().start()
    roles = ("host", "marek", "kuba")
    logs = {role: out / f"room-{role}.jsonl" for role in roles}
    code_file = out / "room-code.txt"
    for stale in [code_file, *out.glob("room-ready-*")]:
        stale.unlink(missing_ok=True)
    procs = {}
    try:
        for role in roles:
            procs[role] = subprocess.Popen(
                [sys.executable, str(HERE / "room_gui_harness.py"), role, str(FILM), str(logs[role]), relay.url,
                 str(code_file), str(out / "ui")], env=ENV, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace")
        outputs = {role: proc.communicate(timeout=180)[0] for role, proc in procs.items()}
    finally:
        for proc in procs.values():
            if proc.poll() is None:
                proc.kill()
        relay.stop()
    (out / "room.log").write_text("\n----\n".join(f"[{r}]\n{o}" for r, o in outputs.items()), encoding="utf-8")
    if not all(p.exists() for p in logs.values()):
        print("room: a log is missing (an app didn't run its script)")
        return False
    data = {role: _load(logs[role]) for role in roles}
    start = min(t for role in roles for t, _ in data[role][1])
    began = {role: data[role][0][0][0] for role in roles if data[role][0]}
    if began:
        spread = max(began.values()) - min(began.values())
        print(f"room: the three scripts started within {spread * 1000:.0f} ms of each other")
    events = {}
    for t, name in sorted(e for role in roles for e in data[role][1]):
        events.setdefault(name, t - start)  # first time each event happened
    needed = ("play", "pause", "seek5", "play2", "jump_playing", "syncup", "marek:join", "kuba:join", "marek:drift",
              "marek:report_gap", "marek:syncme", "marek:friend_pause")
    missing = [n for n in needed if n not in events]
    if missing:
        print(f"room: events missing: {missing}")
        return False

    def gap_at(guest, seconds):
        t = start + seconds
        h, g = _position(data["host"][0], t), _position(data[guest][0], t)
        return None if h is None or g is None else h - g

    def worst(guest, a, b):
        gaps = [abs(g) for g in (gap_at(guest, a + i * 0.25) for i in range(int((b - a) * 4))) if g is not None]
        return max(gaps) if gaps else float("inf")

    joined = max(events["marek:join"], events["kuba:join"])
    limits = {   # (from, to) seconds after the start, allowed gap in ms
        "joined, paused": ((joined + 3.0, events["play"] - 0.2), 150),
        "playing together": ((events["play"] + 1.0, events["pause"] - 0.2), 400),
        "paused together": ((events["pause"] + 0.5, events["seek5"] - 0.2), 150),
        "after a jump": ((events["play2"] + 1.0, events["jump_playing"] - 0.2), 400),
        "jump while playing": ((events["jump_playing"] + 1.5, events["marek:drift"] - 0.2), 150),
        "after Sync up": ((events["syncup"] + 4.5, min(events["marek:friend_pause"],
                                                       events.get("skipspam", float("inf"))) - 0.2), 250),
    }
    ok = True
    host_people = [n for t, n in data["host"][2] if t - start > events["play"]]
    passed = bool(host_people) and max(host_people) == 2
    ok &= passed
    print(f"room: everyone got in (host sees {max(host_people) if host_people else 0} others) "
          f"{'OK' if passed else 'FAIL'}")
    for guest in ("marek", "kuba"):
        for name, ((a, b), limit) in limits.items():
            value = worst(guest, a, b)
            passed = value <= limit
            ok &= passed
            print(f"room: {guest:5s} {name:18s} worst gap {value:7.0f} ms (limit {limit}) {'OK' if passed else 'FAIL'}")
    drift_gap = gap_at("marek", events["marek:report_gap"])
    passed = drift_gap is not None and 2000 < abs(drift_gap) < 3000
    ok &= passed
    print(f"room: Marek's simulated drift stays (no auto-correction): {drift_gap:.0f} ms {'OK' if passed else 'FAIL'}")
    kuba_then = gap_at("kuba", events["marek:report_gap"])
    passed = kuba_then is not None and abs(kuba_then) < 400
    ok &= passed
    print(f"room: Kuba isn't affected by Marek's drift: {kuba_then:.0f} ms {'OK' if passed else 'FAIL'}")
    after_sync_me = gap_at("marek", events["marek:syncme"] + 1.5)
    passed = after_sync_me is not None and abs(after_sync_me) < 400
    ok &= passed
    print(f"room: after Marek's Sync me: {after_sync_me:.0f} ms {'OK' if passed else 'FAIL'}")
    return ok


def check_update(out: Path) -> bool:
    result = subprocess.run([sys.executable, str(HERE / "update_e2e.py"), str(out / "update")], env=ENV,
                            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=600)
    (out / "update.log").write_text(result.stdout + "\n" + result.stderr, encoding="utf-8")
    print("\n".join(line for line in result.stdout.splitlines() if line.startswith(("CHECKS", "update found", "staged", "update window"))))
    if result.returncode != 0:
        print("update check failed; end of its output:")
        print("\n".join((result.stdout + "\n" + result.stderr).strip().splitlines()[-60:]))
    return result.returncode == 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("which", nargs="?", default="all", choices=["ui", "library", "room", "update", "all"])
    parser.add_argument("--out", default=str(Path(tempfile.gettempdir()) / "syncwatch-gui-checks"))
    args = parser.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    checks = {"ui": check_ui, "library": check_library, "room": check_room, "update": check_update}
    chosen = list(checks) if args.which == "all" else [args.which]
    results = {}
    for name in chosen:
        print(f"== {name} ==", flush=True)
        try:
            results[name] = checks[name](out)
        except Exception as exc:  # a crashed check is a failed check
            print(f"{name}: crashed: {type(exc).__name__}: {exc}")
            results[name] = False
    print("RESULTS:", results)
    return 0 if all(results.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
