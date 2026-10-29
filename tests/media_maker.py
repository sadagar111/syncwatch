"""Small test films made on the spot with PyAV: any length, any sound format.

    make_film(path, seconds=3, audio_codec="ac3", channels=6)

The picture is a moving gradient (H.264 when PyAV has libx264, else MPEG-4), the sound
a quiet tone. Optional ``subtitles`` adds a text subtitle track. Used by the library and
converter tests, so they don't depend on big sample films.
"""
from __future__ import annotations

import math
from array import array
from pathlib import Path
from typing import Optional

LAYOUTS = {1: "mono", 2: "stereo", 6: "5.1"}


def make_film(path: Path, seconds: float = 3.0, audio_codec: Optional[str] = "aac", channels: int = 2,
              size=(160, 90), rate: int = 24, sample_rate: int = 48000, language: str = "eng") -> str:
    """Write the film; returns the video codec used."""
    import av
    try:
        av.codec.Codec("libx264", "w")
        video_codec = "libx264"
    except Exception:  # noqa: BLE001 - PyAV built without x264
        video_codec = "mpeg4"
    width, height = size
    with av.open(str(path), "w", format="matroska") as out:
        video = out.add_stream(video_codec, rate=rate)
        video.width, video.height, video.pix_fmt = width, height, "yuv420p"
        if video_codec == "libx264":
            video.options = {"preset": "ultrafast"}
        audio = None
        if audio_codec:
            audio = out.add_stream(audio_codec, rate=sample_rate, layout=LAYOUTS[channels])
            audio.metadata["language"] = language
        frames = int(seconds * rate)
        for index in range(frames):
            frame = av.VideoFrame(width, height, "yuv420p")
            shade = (index * 7) % 200 + 20
            frame.planes[0].update(bytes([(shade + x) % 256 for x in range(width)] * height))
            frame.planes[1].update(bytes([128]) * frame.planes[1].buffer_size)
            frame.planes[2].update(bytes([90 + index % 60]) * frame.planes[2].buffer_size)
            frame.pts = index
            for packet in video.encode(frame):
                out.mux(packet)
        for packet in video.encode(None):
            out.mux(packet)
        if audio is not None:
            block = audio.codec_context.frame_size or 1024
            total = int(seconds * sample_rate)
            for start in range(0, total, block):
                count = min(block, total - start)
                frame = av.AudioFrame(format="fltp", layout=LAYOUTS[channels], samples=block)
                tone = array("f", (0.1 * math.sin(2 * math.pi * 440 * (start + i) / sample_rate) if i < count else 0.0
                                   for i in range(block)))
                for plane in frame.planes:
                    plane.update(tone.tobytes())
                frame.sample_rate = sample_rate
                frame.pts = start
                for packet in audio.encode(frame):
                    out.mux(packet)
            for packet in audio.encode(None):
                out.mux(packet)
    return "h264" if video_codec == "libx264" else "mpeg4"
