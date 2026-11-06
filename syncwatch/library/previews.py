"""Pictures for the seek bar: small frames of a film, made once in a process of its own.

    python previews.py <film> <out file> [--every 10] [--width 160] [--max 400]

Jumps through the film to the keyframe before every point (fast: nothing else is decoded), one
point every ``--every`` seconds (further apart for long films: at most ``--max`` pictures), shrinks
each picture to ``--width`` (the height follows the film's shape) and stores it as a small JPEG
(made by FFmpeg's own encoder: about 2-6 KB each). The file:

    b"SWPV1\\n"
    one line of JSON: {"width", "height", "step", "count", "duration", "sizes": [bytes of each JPEG]}
    the JPEGs, one after the other

It's written to "<out file>.part" and renamed at the end. The window shows picture ``i`` for the
moment ``(i + 0.5) * step`` (PreviewFile below reads it; no Qt needed for that part).

Output: one JSON object per line on stdout: {"start": {...}}, {"progress": 0.4}, {"done": "<out>"}
or {"error": "...", "detail": "..."}. Standard library and PyAV only, its own process at a low
priority: a strange file can't take SyncWatch down, and playing the film goes first.
"""
from __future__ import annotations

import json
import os
import sys
from fractions import Fraction
from pathlib import Path
from typing import List, Optional

MAGIC = b"SWPV1\n"
MAX_HEADER = 64 * 1024


def command(*arguments: str) -> list:
    """How the window starts this file: SyncWatch's own Python, ignoring outside settings."""
    flags = ["-E"] + (["-P"] if sys.version_info >= (3, 11) else [])
    return [sys.executable, *flags, str(Path(__file__).resolve()), *map(str, arguments)]


def say(**message) -> None:
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


def plan(duration: float, every: float, maximum: int) -> List[float]:
    """The moments to take a picture of: the middle of every step."""
    if duration <= 0 or every <= 0 or maximum <= 0:
        return []
    step = min(duration, max(every, duration / maximum))     # (a film shorter than a step: one picture)
    count = max(1, min(maximum, int(duration / step)))
    return [(index + 0.5) * step for index in range(count)]


def _lower_priority() -> None:
    try:
        if sys.platform == "win32":
            import ctypes  # noqa: PLC0415
            kernel = ctypes.windll.kernel32
            kernel.SetPriorityClass(kernel.GetCurrentProcess(), 0x00004000)   # below normal
        else:
            os.nice(10)
    except (OSError, AttributeError):
        pass


def make(source: str, target: str, every: float = 10.0, width: int = 160, maximum: int = 400) -> None:
    import av  # noqa: PLC0415 - only in this process

    with av.open(source) as container:
        video = next((s for s in container.streams.video
                      if not (s.disposition & getattr(av.stream.Disposition, "attached_pic", 0))), None)
        if video is None or not video.width or not video.height:
            raise ValueError("no picture in this file")
        video.thread_type = "AUTO"
        duration = (container.duration or 0) / 1_000_000
        if duration <= 0 and video.duration and video.time_base:
            duration = float(video.duration * video.time_base)
        aspect = video.width / video.height
        ratio = video.sample_aspect_ratio
        if ratio and ratio.numerator and ratio.denominator:
            aspect *= float(ratio)
        width = max(32, int(width) // 2 * 2)
        height = max(18, int(round(width / aspect / 2)) * 2)
        times = plan(duration, every, maximum)
        step = times[1] - times[0] if len(times) > 1 else duration
        say(start={"duration": duration, "count": len(times), "width": width, "height": height})
        encoder = av.CodecContext.create("mjpeg", "w")
        encoder.width, encoder.height = width, height
        encoder.pix_fmt = "yuvj420p"
        encoder.time_base = Fraction(1, 25)
        pictures: List[bytes] = []
        last = b""
        for index, moment in enumerate(times):
            picture = b""
            try:
                container.seek(int(moment / video.time_base), stream=video, backward=True)
                frame = next(container.decode(video))
                small = frame.reformat(width=width, height=height, format="yuvj420p")
                small.pts, small.time_base = index, encoder.time_base
                picture = b"".join(bytes(packet) for packet in encoder.encode(small))
            except (av.FFmpegError, StopIteration, ValueError, OverflowError):
                picture = b""
            picture = picture or last        # a moment that couldn't be read shows the one before
            pictures.append(picture)
            last = picture
            if index % 10 == 9:
                say(progress=round((index + 1) / len(times), 3))
    header = {"width": width, "height": height, "step": step, "count": len(pictures), "duration": duration,
              "sizes": [len(picture) for picture in pictures]}
    part = Path(target + ".part")
    with open(part, "wb") as out:
        out.write(MAGIC)
        out.write(json.dumps(header).encode("utf-8") + b"\n")
        for picture in pictures:
            out.write(picture)
    os.replace(part, target)


class PreviewFile:
    """Reads a previews file: picture bytes for a moment (checked: sizes must add up)."""

    def __init__(self, path: Path):
        data = Path(path).read_bytes()
        if not data.startswith(MAGIC):
            raise ValueError("not a previews file")
        end = data.find(b"\n", len(MAGIC), len(MAGIC) + MAX_HEADER)
        if end < 0:
            raise ValueError("no header")
        header = json.loads(data[len(MAGIC):end].decode("utf-8"))
        sizes = header.get("sizes")
        if not isinstance(sizes, list) or not all(isinstance(n, int) and 0 <= n < 1_000_000 for n in sizes):
            raise ValueError("odd sizes")
        if end + 1 + sum(sizes) != len(data):
            raise ValueError("sizes don't add up")
        self.width, self.height = int(header["width"]), int(header["height"])
        self.step, self.duration = float(header["step"]), float(header["duration"])
        self._data, self._offsets = data, []
        offset = end + 1
        for size in sizes:
            self._offsets.append((offset, size))
            offset += size

    @property
    def count(self) -> int:
        return len(self._offsets)

    def index_at(self, seconds: float) -> int:
        if not self._offsets or self.step <= 0:
            return -1
        return max(0, min(self.count - 1, int(seconds / self.step)))

    def picture(self, index: int) -> Optional[bytes]:
        if not 0 <= index < self.count:
            return None
        offset, size = self._offsets[index]
        return self._data[offset:offset + size] or None


def main(argv) -> int:
    import argparse  # noqa: PLC0415
    parser = argparse.ArgumentParser(prog="previews.py", description=__doc__.split("\n\n")[0])
    parser.add_argument("film")
    parser.add_argument("out")
    parser.add_argument("--every", type=float, default=10.0)
    parser.add_argument("--width", type=int, default=160)
    parser.add_argument("--max", type=int, default=400)
    args = parser.parse_args(argv)
    _lower_priority()
    try:
        import av  # noqa: F401, PLC0415
    except ImportError as exc:
        say(error="PyAV isn't installed", detail=str(exc))
        return 1
    try:
        make(args.film, args.out, max(1.0, args.every), max(32, min(args.width, 480)), max(1, min(args.max, 2000)))
    except Exception as exc:  # noqa: BLE001 - whatever FFmpeg says ends up here
        say(error="couldn't make the pictures", detail=f"{type(exc).__name__}: {exc}"[:1000])
        return 1
    say(done=args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
