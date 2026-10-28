"""The real-app checks (offscreen): the whole window, two apps in sync, the update cycle.

    python tests/gui_checks.py [ui|pair|update|all] [--out FOLDER]

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
import time
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
                                                                                     "subtitle sources"))))
    return result.returncode == 0


def _load(path: Path):
    samples, events = [], []
    for line in path.read_text().splitlines():
        entry = json.loads(line)
        if "pos" in entry:
            samples.append((entry["t"], entry["pos"]))
        elif "event" in entry and "t" in entry:
            events.append((entry["t"], entry["event"]))
    return samples, events


def _position(samples, t):
    times = [s[0] for s in samples]
    i = bisect.bisect_left(times, t)
    if i <= 0 or i >= len(samples):
        return None
    (t0, p0), (t1, p1) = samples[i - 1], samples[i]
    return p0 + (p1 - p0) * (t - t0) / (t1 - t0) if t1 > t0 else p0


def check_pair(out: Path) -> bool:
    """Two real apps (host + friend) with real video players, driven by tests/pair_harness.py."""
    logs = {role: out / f"pair-{role}.jsonl" for role in ("host", "friend")}
    port = "50661"
    host = subprocess.Popen([sys.executable, str(HERE / "pair_harness.py"), "host", str(FILM), str(logs["host"]), port],
                            env=ENV, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")
    time.sleep(1.0)
    friend = subprocess.run([sys.executable, str(HERE / "pair_harness.py"), "friend", str(FILM), str(logs["friend"]),
                             port], env=ENV, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300)
    host_out, _ = host.communicate(timeout=120)
    (out / "pair.log").write_text(host_out + "\n----\n" + friend.stdout + friend.stderr, encoding="utf-8")
    if not all(p.exists() for p in logs.values()):
        print("pair: a log is missing (an app didn't run its script)")
        return False
    host_samples, host_events = _load(logs["host"])
    friend_samples, friend_events = _load(logs["friend"])
    start = min(t for t, _ in host_events + friend_events)
    events = {}
    for t, name in sorted(host_events + friend_events):
        events.setdefault(name, t - start)  # first time each event happened

    def gap_at(seconds):
        t = start + seconds
        h, f = _position(host_samples, t), _position(friend_samples, t)
        return None if h is None or f is None else h - f

    def worst(a, b):
        gaps = [abs(g) for g in (gap_at(a + i * 0.25) for i in range(int((b - a) * 4))) if g is not None]
        return max(gaps) if gaps else float("inf")

    limits = {   # (from, to) seconds after the start of the script, allowed gap in ms
        "joined, paused": ((events["join"] + 1.5, events["play"] - 0.2), 120),
        "playing together": ((events["play"] + 1.0, events["pause"] - 0.2), 400),
        "paused together": ((events["pause"] + 0.5, events["seek5"] - 0.2), 150),
        "after a jump": ((events["play2"] + 1.0, events["drift"] - 0.2), 400),
        "after Sync up": ((events["syncup"] + 4.5, events["friend_pause"] - 0.2), 250),
    }
    ok = True
    for name, ((a, b), limit) in limits.items():
        value = worst(a, b)
        passed = value <= limit
        ok &= passed
        print(f"pair: {name:18s} worst gap {value:7.0f} ms (limit {limit}) {'OK' if passed else 'FAIL'}")
    drift_gap = gap_at(events["report_gap"])
    passed = drift_gap is not None and 2000 < abs(drift_gap) < 3000
    ok &= passed
    print(f"pair: simulated drift stays (no auto-correction): {drift_gap:.0f} ms {'OK' if passed else 'FAIL'}")
    after_sync_me = gap_at(events["syncme"] + 1.5)
    passed = after_sync_me is not None and abs(after_sync_me) < 400
    ok &= passed
    print(f"pair: after Sync me: {after_sync_me:.0f} ms {'OK' if passed else 'FAIL'}")
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
    parser.add_argument("which", nargs="?", default="all", choices=["ui", "pair", "update", "all"])
    parser.add_argument("--out", default=str(Path(tempfile.gettempdir()) / "syncwatch-gui-checks"))
    args = parser.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    checks = {"ui": check_ui, "pair": check_pair, "update": check_update}
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
