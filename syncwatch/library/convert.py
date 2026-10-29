"""SyncWatch's built-in converter: a playable copy of a film whose sound can't be played.

    python convert.py probe <film>
    python convert.py convert <film> <new file> [--all-audio]

What "convert" does, in one pass through the film:
* the picture (video) is copied exactly as it is, frame for frame: nothing is re-encoded,
  so it's fast and the quality stays the same
* sound tracks in formats that may not play (AC3, E-AC3, DTS, TrueHD, ...) are converted
  to AAC, which every player plays; AAC, MP3, Opus, Vorbis, FLAC and PCM are kept as
  they are (``--all-audio`` converts those too). A sound track that can't be read at
  all can't be converted either, so it's left out.
* subtitle tracks (text and picture ones) and chapters are kept; things a film player
  doesn't need (attached fonts, cover pictures, data tracks) are left out
* the result is an MKV file. It's written to "<new file>.part" and renamed at the very
  end, so a cancelled conversion never leaves a half-finished film behind.

Timestamps are kept as they are, so picture and sound stay in sync, and the copy is as
long as the original (friends with the original file and friends with the converted copy
still line up: SyncWatch compares films by length).

Output: one JSON object per line on stdout, for the window to show progress:
    {"start": {"duration": 7210.5, "tracks": [...]}}
    {"progress": 0.42, "done_s": 3028.4}
    {"done": "<new file>"}         or        {"error": "...", "detail": "..."}

This file only uses Python's standard library and PyAV (FFmpeg for Python), and runs as
its own process (SyncWatch starts it with its own Python): if FFmpeg ever crashes on a
strange file, only the conversion stops, not SyncWatch.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

KEEP_AUDIO = {"aac", "mp3", "opus", "vorbis", "flac"}          # plus every pcm_* format
MKV_SUBTITLES = {"subrip", "srt", "ass", "ssa", "webvtt", "text", "hdmv_pgs_subtitle", "pgssub",
                 "dvd_subtitle", "dvdsub", "dvb_subtitle", "dvbsub"}
DECODE_TEST_PACKETS = 400   # packets read per track when checking that it decodes
PROGRESS_EVERY_S = 0.5


def command(*arguments: str) -> list:
    """How SyncWatch starts this file: with its own Python, ignoring PYTHON* environment
    variables (-E) and, on Python 3.11+, without putting this folder on the import path (-P).

    Not -I ("isolated"): that also hides libraries installed with "pip install --user", which
    is where the Microsoft Store's Python puts them, so PyAV would look missing (SW-225)."""
    flags = ["-E"] + (["-P"] if sys.version_info >= (3, 11) else [])
    return [sys.executable, *flags, str(Path(__file__).resolve()), *map(str, arguments)]


def say(**message) -> None:
    # ASCII only (other letters as \u escapes): the window reads it the same way whatever the
    # PC's language settings, even for film names in any alphabet.
    print(json.dumps(message, ensure_ascii=True), flush=True)


def codec_name(stream) -> str:
    context = stream.codec_context
    return (context.name if context is not None else "") or ""


def keeps_audio(name: str) -> bool:
    return name in KEEP_AUDIO or name.startswith("pcm_")


def is_cover_picture(av, stream) -> bool:
    try:
        return bool(stream.disposition & av.stream.Disposition.attached_pic)
    except (AttributeError, TypeError):
        return False


# =============================================================== probe
def probe(path: str) -> dict:
    """What's in the film, and whether each video and sound track can be decoded."""
    import av
    with av.open(path) as container:
        tracks = []
        for stream in container.streams:
            context = stream.codec_context
            entry = {"index": stream.index, "type": stream.type, "codec": codec_name(stream),
                     "language": stream.metadata.get("language", ""), "title": stream.metadata.get("title", "")}
            if stream.type == "audio" and context is not None:
                entry["channels"] = context.channels
            if stream.type == "video" and context is not None:
                entry["size"] = f"{context.width}x{context.height}"
                entry["cover"] = is_cover_picture(av, stream)
            tracks.append(entry)
        duration = container.duration / 1_000_000 if container.duration else 0.0
        format_name = container.format.name
    for entry in tracks:
        if entry["type"] in ("video", "audio") and not entry.get("cover"):
            entry["decodes"] = _decodes(path, entry["index"])
    pictures = [t for t in tracks if t["type"] == "video" and not t.get("cover")]
    return {"format": format_name, "duration": duration, "tracks": tracks,
            "video_ok": any(t.get("decodes") for t in pictures),
            "audio_ok": all(t.get("decodes") for t in tracks if t["type"] == "audio")}


def _decodes(path: str, index: int) -> bool:
    """Does at least one picture / piece of sound come out of track ``index``?"""
    import av
    try:
        with av.open(path) as container:
            stream = container.streams[index]
            if stream.codec_context is None:
                return False
            for count, packet in enumerate(container.demux(stream)):
                if count >= DECODE_TEST_PACKETS:
                    return False
                try:
                    if packet.decode():
                        return True
                except av.FFmpegError:
                    continue   # a damaged packet at the start: try the next ones
    except (av.FFmpegError, OSError, ValueError, IndexError):
        return False
    return False


# =============================================================== convert
def convert(source: str, target: str, all_audio: bool = False) -> None:
    import av
    part = target + ".part"
    started = time.monotonic()
    try:
        with av.open(source) as inp, av.open(part, "w", format="matroska") as out:
            plan, described = _plan(av, inp, out, all_audio, source)
            if not any(action[0] == "copy" and stream.type == "video"
                       for stream, action in ((inp.streams[i], a) for i, a in plan.items())):
                raise ValueError("the film has no picture SyncWatch can copy")
            duration = inp.duration / 1_000_000 if inp.duration else 0.0
            say(start={"duration": duration, "tracks": described})
            out.metadata.update({k: v for k, v in inp.metadata.items() if k.lower() != "encoder"})
            try:
                chapters = inp.chapters()
                if chapters:
                    out.set_chapters(chapters)
            except (AttributeError, av.FFmpegError, ValueError, TypeError):
                pass   # chapters are nice to have, never a reason to fail
            _run(av, inp, out, plan, duration)
        os.replace(part, target)
    except BaseException:
        try:
            os.remove(part)
        except OSError:
            pass
        raise
    say(done=target, seconds=round(time.monotonic() - started, 1))


def _plan(av, inp, out, all_audio: bool, source: str):
    """What happens to each track: ("copy", out stream) or ("aac", out stream, resampler).

    A sound track that can't be decoded at all can't be converted either: it's left out."""
    plan, described = {}, []
    for stream in inp.streams:
        name = codec_name(stream)
        language = stream.metadata.get("language", "")
        action = "skip"
        if stream.type == "video" and not is_cover_picture(av, stream):
            plan[stream.index] = ("copy", out.add_stream_from_template(stream))
            action = "copy"
        elif stream.type == "audio" and stream.codec_context is not None:
            if keeps_audio(name) and not all_audio:
                plan[stream.index] = ("copy", out.add_stream_from_template(stream))
                action = "copy"
            elif _decodes(source, stream.index):
                plan[stream.index] = _aac_stream(av, stream, out)
                action = "to AAC"
            else:
                action = "left out (can't be read)"
        elif stream.type == "subtitle" and name in MKV_SUBTITLES:
            plan[stream.index] = ("copy", out.add_stream_from_template(stream))
            action = "copy"
        if stream.index in plan:
            target = plan[stream.index][1]
            target.metadata.update({k: v for k, v in stream.metadata.items()
                                    if k.upper() not in ("ENCODER", "DURATION", "NUMBER_OF_FRAMES",
                                                         "NUMBER_OF_BYTES", "BPS", "_STATISTICS_TAGS",
                                                         "_STATISTICS_WRITING_APP", "_STATISTICS_WRITING_DATE_UTC")})
            try:
                target.disposition = stream.disposition
            except (AttributeError, TypeError, ValueError):
                pass
        described.append({"index": stream.index, "type": stream.type, "codec": name, "language": language,
                          "action": action})
    return plan, described


def _aac_stream(av, stream, out):
    context = stream.codec_context
    channels = context.channels or 2
    layout = "mono" if channels == 1 else "stereo" if channels == 2 else "5.1"
    rate = context.sample_rate or 48000
    encoder = out.add_stream("aac", rate=rate, layout=layout)
    encoder.bit_rate = {"mono": 128_000, "stereo": 192_000}.get(layout, 384_000)
    resampler = av.AudioResampler(format="fltp", layout=layout, rate=rate, frame_size=1024)
    return ("aac", encoder, resampler)


def _run(av, inp, out, plan, duration: float) -> None:
    last_report = 0.0
    done_s = 0.0
    for packet in inp.demux([inp.streams[index] for index in plan]):
        action = plan[packet.stream.index]
        if packet.pts is not None and packet.time_base is not None:
            done_s = max(done_s, float(packet.pts * packet.time_base))
        if action[0] == "copy":
            if packet.size == 0:   # the empty packet at the end of each track
                continue
            packet.stream = action[1]
            out.mux(packet)
        else:
            _, encoder, resampler = action
            try:
                frames = packet.decode()
            except av.FFmpegError:
                continue   # a damaged piece of sound: skip it, like a player would
            for frame in frames:
                for resampled in resampler.resample(frame):
                    out.mux(encoder.encode(resampled))
        now = time.monotonic()
        if now - last_report >= PROGRESS_EVERY_S:
            last_report = now
            say(progress=round(min(1.0, done_s / duration), 4) if duration else 0.0, done_s=round(done_s, 1))
    for action in plan.values():
        if action[0] == "aac":
            _, encoder, resampler = action
            for resampled in resampler.resample(None):
                out.mux(encoder.encode(resampled))
            out.mux(encoder.encode(None))
    say(progress=1.0, done_s=round(duration, 1))


# =============================================================== command line
def main(argv) -> int:
    if len(argv) >= 2 and argv[0] == "probe":
        try:
            say(probe=probe(argv[1]))
            return 0
        except Exception as exc:  # noqa: BLE001 - reported to the window, which explains it
            say(error="probe", detail=f"{type(exc).__name__}: {exc}")
            return 1
    if len(argv) >= 3 and argv[0] == "convert":
        try:
            convert(argv[1], argv[2], all_audio="--all-audio" in argv[3:])
            return 0
        except Exception as exc:  # noqa: BLE001 - reported to the window, which explains it
            say(error="convert", detail=f"{type(exc).__name__}: {exc}")
            return 1
    print(__doc__.split("\n\n")[1], file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
