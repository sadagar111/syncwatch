"""How long do jumps take on this PC? (For tuning "When someone jumps while the film plays".)

    python tools/measure_jumps.py <film> [--jumps 8] [--player mpv|qt]
    (or drag a film onto tools\\measure_jumps.bat)

Plays the film without sound and without a window, jumps to a few spots while it plays,
and reports for each jump:
  * how long until the picture at the new spot was there, and
  * whether the film's clock kept running meanwhile (then the picture catches up and the PC
    isn't behind afterwards) or waited (then the PC ends up behind by the loading time).

--player: mpv (SyncWatch's player from 0.4.2, the default) or qt (the fallback player).

Nothing is saved or changed. Send the summary at the end to whoever looks after SyncWatch.
"""
from __future__ import annotations

import argparse
import random
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from PySide6.QtCore import QTimer  # noqa: E402
from PySide6.QtMultimedia import QVideoSink  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from syncwatch import mpvlib  # noqa: E402
from syncwatch.ui.player import Player  # noqa: E402

GAP_S = 4.0          # between jumps
FIRST_S = 3.0        # play this long before the first jump


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Measure how long jumps take on this PC.")
    parser.add_argument("film", help="a film file (MKV, MP4, ...)")
    parser.add_argument("--jumps", type=int, default=8, help="how many jumps (default 8)")
    parser.add_argument("--player", choices=["mpv", "qt"], default="mpv", help="which video player (default mpv)")
    args = parser.parse_args(argv)
    film = Path(args.film.strip('"'))
    if not film.is_file():
        print(f"Can't find the film: {film}")
        return 2
    count = max(2, min(30, args.jumps))
    app = QApplication(sys.argv[:1])
    frames = []      # (when, film time in ms) of every picture
    if args.player == "mpv":
        from syncwatch.ui.mpv_player import MpvPlayer
        try:
            player = MpvPlayer(mpvlib.load(), "none")
        except mpvlib.Unavailable as exc:
            print(f"mpv isn't available here ({exc}). Try --player qt.")
            return 2
        player.video_widget("#000000")
        player.start()
        player.set_volume(0, True)

        def sample():   # mpv's position is the picture on screen (it waits for the picture after a jump)
            if player.is_ready() and player.is_playing():
                frames.append((time.monotonic(), player.position()))
        sampler = QTimer()
        sampler.timeout.connect(sample)
        sampler.start(20)
    else:
        player = Player()
        sink = QVideoSink()
        player.mp.setVideoSink(sink)
        player.attach_sink(sink)
        sink.videoFrameChanged.connect(lambda f: frames.append((time.monotonic(), f.startTime() / 1000.0))
                                       if f.isValid() and f.startTime() >= 0 else None)
        player.audio.setMuted(True)
    if not player.load(film):
        print("SyncWatch can't open that film.")
        return 2
    jumps = []       # (when, target ms)
    state = {"start": None, "next": None}
    rng = random.Random(7)
    total = FIRST_S + count * GAP_S

    def step():
        now = time.monotonic()
        if state["start"] is None:
            if player.has_media() and player.duration() > 0:
                state["start"] = now
                state["next"] = now + FIRST_S
                player.play()
                print(f"{film.name}: {player.duration() / 60000:.0f} min, player: {player.describe()}. "
                      f"Measuring {count} jumps, about {total:.0f} s…", flush=True)
            elif now - started > 20:
                print("The film didn't load within 20 s.")
                app.exit(1)
            return
        if len(jumps) < count and now >= state["next"]:
            duration = player.duration()
            target = rng.uniform(0.05, 0.9) * duration
            jumps.append((now, target))
            player.seek(target)
            state["next"] = now + GAP_S
            left = total - (now - state["start"])
            print(f"  jump {len(jumps)} of {count} to {target / 60000:.1f} min · about {left:.0f} s left",
                  flush=True)
        elif len(jumps) == count and now >= state["next"]:
            player.tick()
            app.exit(report(jumps, frames))
        player.tick()

    started = time.monotonic()
    timer = QTimer()
    timer.timeout.connect(step)
    timer.start(10)
    code = app.exec()
    player.shutdown()
    return code


def report(jumps, frames) -> int:
    loads, behinds = [], []
    print()
    for i, (when, target) in enumerate(jumps):
        end = jumps[i + 1][0] if i + 1 < len(jumps) else float("inf")
        pictures = [(t, ms) for t, ms in frames if when < t < end and abs(ms - target) < 15_000]
        near = [(t, ms) for t, ms in pictures if abs(ms - target) < 2500]
        if not near:
            print(f"  jump {i + 1}: no picture near the new spot")
            continue
        load = (near[0][0] - when) * 1000
        settled = [(target + (t - when) * 1000) - ms for t, ms in pictures if 2.5 < t - when < 3.8]
        behind = statistics.median(settled) if settled else float("nan")
        loads.append(load)
        if settled:
            behinds.append(behind)
        print(f"  jump {i + 1}: picture after {load:5.0f} ms; 3 s later {behind:5.0f} ms behind")
    if not loads:
        print("No jump could be measured.")
        return 1
    print()
    print(f"SUMMARY: jumps take {statistics.median(loads):.0f} ms (median), {max(loads):.0f} ms at most; "
          f"3 s after a jump the film is {statistics.median(behinds) if behinds else float('nan'):.0f} ms "
          "behind where it should be (about 0 = the player catches up by itself).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
