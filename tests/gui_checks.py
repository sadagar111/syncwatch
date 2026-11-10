"""The real-app checks (offscreen): the whole window, the film library, three apps in one room,
the update cycle.

    python tests/gui_checks.py [ui|library|youtube|discover|room|update|all] [--out FOLDER]

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
import time
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
FILM = HERE / "media" / "demo.mkv"
ENV = dict(os.environ, QT_QPA_PLATFORM="offscreen", SYNCWATCH_FAULTS_TO_STDERR="1")   # a crash shows where
if sys.platform.startswith("linux"):
    ENV.setdefault("QT_AUDIO_BACKEND", "pulseaudio")  # avoids a PipeWire crash without a sound server


def check_ui(out: Path) -> bool:
    result = subprocess.run([sys.executable, str(HERE / "ui_harness.py"), "normal", str(out / "ui"), str(FILM)],
                            env=ENV, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300)
    (out / "ui.log").write_text(result.stdout + "\n" + result.stderr, encoding="utf-8")
    print("\n".join(line for line in result.stdout.splitlines() if line.startswith(("KEY CHECKS", "UI HARNESS",
                                                                                     "ROOM CHECKS", "CHAT CHECKS", "PREVIEW CHECKS", "PLAYER",
                                                                                     "subtitle sources"))))
    player = next((line[len("PLAYER:"):].strip() for line in result.stdout.splitlines()
                   if line.startswith("PLAYER:")), "")
    if player and os.environ.get("GITHUB_ACTIONS"):   # shown on the pull request page
        print(f"::notice title=Video player in the app check ({sys.platform})::{player}", flush=True)
    if result.returncode != 0:
        print(f"ui check failed (exit code {result.returncode}); end of its output:")
        print("\n".join((result.stdout + "\n" + result.stderr).strip().splitlines()[-40:]))
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


def check_youtube(out: Path) -> bool:
    """A pasted link, the queue moving on by itself, Skip, a deleted video (YouTube itself replaced by
    a local web server; the resolver process, yt-dlp and mpv are real)."""
    result = subprocess.run([sys.executable, str(HERE / "youtube_harness.py"), str(out / "youtube")], env=ENV,
                            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=600)
    (out / "youtube.log").write_text(result.stdout + "\n" + result.stderr, encoding="utf-8")
    print("\n".join(line for line in result.stdout.splitlines()
                    if line.startswith(("YOUTUBE", "activity", "ERRORS SEEN"))))
    if result.returncode != 0:
        print(f"youtube check failed (exit code {result.returncode}); end of its output:")
        print("\n".join((result.stdout + "\n" + result.stderr).strip().splitlines()[-40:]))
    return result.returncode == 0


def check_discover(out: Path) -> bool:
    """What should we watch? (0.9): ideas from the library, posters, details, the trailer, the film
    vote in a room, playing the winner (TMDB replaced by tests/fake_tmdb.py, a relay on this PC)."""
    result = subprocess.run([sys.executable, str(HERE / "discover_harness.py"), str(out / "discover")], env=ENV,
                            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=600)
    (out / "discover.log").write_text(result.stdout + "\n" + result.stderr, encoding="utf-8")
    print("\n".join(line for line in result.stdout.splitlines() if line.startswith(("DISCOVER", "ERRORS SEEN"))))
    if result.returncode != 0:
        print(f"discover check failed (exit code {result.returncode}); end of its output:")
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
    # Each app's output goes to a file, not a pipe: a pipe nobody reads yet (they were read one after
    # the other) fills up (4 KB on Windows) and then blocks that app until the one before it has ended.
    printed = {role: out / f"room-{role}.out" for role in roles}
    files = {role: printed[role].open("w", encoding="utf-8") for role in roles}
    try:
        for role in roles:
            procs[role] = subprocess.Popen(
                [sys.executable, str(HERE / "room_gui_harness.py"), role, str(FILM), str(logs[role]), relay.url,
                 str(code_file), str(out / "ui")], env=ENV, stdout=files[role], stderr=subprocess.STDOUT)
        deadline = time.monotonic() + 180
        for proc in procs.values():
            proc.wait(timeout=max(1.0, deadline - time.monotonic()))
    finally:
        for proc in procs.values():
            if proc.poll() is None:
                proc.kill()
        for file in files.values():
            file.close()
        relay.stop()
    outputs = {role: printed[role].read_text(encoding="utf-8", errors="replace") for role in roles}
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
        for role, proc in procs.items():
            print(f"room: {role} exited with {proc.returncode}; end of its output:")
            print("\n".join(outputs[role].strip().splitlines()[-25:]))
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
    ok &= _room_extras(out)
    ok &= _torrent_offer(out)
    ok &= _ready_check(out)
    return ok


def _room_extras(out: Path) -> bool:
    """Chat, a reaction and a film vote between the three apps (0.7 + 0.8), when they have them."""
    got = {}
    for role in ("host", "marek", "kuba"):
        lines = (out / f"room-{role}.jsonl").read_text(encoding="utf-8").splitlines()
        got[role] = [json.loads(line) for line in lines if '"got_' in line]
    ok = True
    expected = {
        "Marek's chat message reached the others": lambda role: any(
            e["event"] == "got_chat_message" and e["args"][:2] == ["Marek", "hello from Marek"] for e in got[role]),
        "Kuba's 🍿 reached the others": lambda role: any(
            e["event"] == "got_reaction" and e["args"][:2] == ["Kuba", "🍿"] for e in got[role]),
        "everyone heard the vote's winner (Inception)": lambda role: any(
            e["event"] == "got_vote_won" and e["args"] == ["Inception"] for e in got[role]),
    }
    senders = {"Marek's chat message reached the others": "marek", "Kuba's 🍿 reached the others": "kuba"}
    for name, test in expected.items():
        roles = [r for r in ("host", "marek", "kuba") if r != senders.get(name)]
        passed = all(test(role) for role in roles)
        ok &= passed
        print(f"room: {name} {'OK' if passed else 'FAIL'}")
    return ok


def _torrent_offer(out: Path) -> bool:
    """(0.9) The host streams a torrent: Marek (ask first, the default) is asked in the sidebar and
    streams it only after his click; Kuba (Settings > Torrents: don't) is neither asked nor streams."""
    seen = {}
    for role in ("marek", "kuba"):
        for line in (out / f"room-{role}.jsonl").read_text(encoding="utf-8").splitlines():
            if ':seen"' in line:
                entry = json.loads(line)
                seen[entry["event"]] = entry
    marek, after, kuba = (seen.get(k, {}) for k in ("marek:offer_state:seen", "marek:offer_state2:seen",
                                                     "kuba:offer_state:seen"))
    ok = True
    for name, passed in (
            ("Marek is asked before the host's torrent streams", marek.get("banner") == "Stream it too"
             and marek.get("streaming") is False),
            ("after his click it streams", after.get("streaming") is True),
            ("Kuba (Settings: don't) isn't asked and streams nothing", kuba.get("banner") == ""
             and kuba.get("streaming") is False)):
        ok &= passed
        print(f"room: {name} {'OK' if passed else 'FAIL'}")
    if not ok:
        print(f"room: what they saw: {seen}")
    return ok


def _ready_check(out: Path) -> bool:
    """(0.9) The host's ready check through the People panel's buttons: both members asked, it starts
    for everyone (Sync up's countdown) once both clicked I'm ready."""
    seen = {}
    for role in ("host", "marek", "kuba"):
        for line in (out / f"room-{role}.jsonl").read_text(encoding="utf-8").splitlines():
            if ':ready:seen"' in line:
                entry = json.loads(line)
                seen.setdefault(role, []).append(entry)
    asked = [entries[0] for role, entries in seen.items() if role != "host" and entries]
    after = [entries[-1] for entries in seen.values() if entries]
    ok = True
    for name, passed in (
            ("both members are asked, with a button", len(asked) == 2 and all(
                e["button"] == "✓ I'm ready" and "everyone ready?" in e["line"] for e in asked)),
            ("once both said so, everyone plays again", len(after) == 3 and all(
                e["playing"] and e["line"] == "" for e in after))):
        ok &= passed
        print(f"room: ready check: {name} {'OK' if passed else 'FAIL'}")
    if not ok:
        print(f"room: what they saw: {seen}")
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
    parser.add_argument("which", nargs="?", default="all",
                        choices=["ui", "library", "youtube", "discover", "room", "update", "all"])
    parser.add_argument("--out", default=str(Path(tempfile.gettempdir()) / "syncwatch-gui-checks"))
    args = parser.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    checks = {"ui": check_ui, "library": check_library, "youtube": check_youtube, "discover": check_discover,
              "room": check_room,
              "update": check_update}
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
