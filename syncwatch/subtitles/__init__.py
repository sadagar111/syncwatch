"""Subtitles: reading files and tracks inside films, and timing lookups.

``parsers`` reads .srt/.vtt/.ass/.ssa files, ``mkv`` reads text tracks inside
.mkv/.webm films, and ``model`` holds the cues and answers "what should be on
screen right now?" with your delay and stretch applied.
"""
from .model import Cue, SubtitleTrack, STRETCH_PRESETS  # noqa: F401
