"""Reading text subtitle tracks stored inside .mkv / .webm files.

Why not let the video library show them? Because then SyncWatch couldn't
apply your delay, stretch, size and colour settings to them. So SyncWatch
reads the subtitle text out of the file itself and draws it like any other
subtitles.

How: an MKV file is a tree of "elements" (EBML). Subtitles are small blocks
spread through the whole file between the video and audio blocks, so the
whole file has to be scanned once. The scan only reads block *headers* and
skips over video/audio data, and it delivers subtitles in film order as it
goes, so the beginning of the film has subtitles almost immediately. The
result is cached in your profile, so each film is only scanned once.

Picture-based tracks (Blu-ray PGS, DVD VobSub) can't be read as text (SW-253).

Safety: the files come from the internet, so every size is checked against
the real file size, nothing large is ever read into memory, compressed
blocks are decompressed with a size cap, and the parser can't loop forever.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO, Callable, Dict, List, Optional, Sequence, Tuple

from ..errors import SyncWatchError
from .model import Cue
from .parsers import ass_to_markup, normalise_language, parse_ass_styles, _plain_cue_text

log = logging.getLogger("syncwatch.subtitles.mkv")

# Element IDs (from the Matroska specification)
EBML = 0x1A45DFA3
DOCTYPE = 0x4282
SEGMENT = 0x18538067
SEEKHEAD, SEEK, SEEKID, SEEKPOSITION = 0x114D9B74, 0x4DBB, 0x53AB, 0x53AC
INFO, TIMESTAMPSCALE, DURATION = 0x1549A966, 0x2AD7B1, 0x4489
TRACKS, TRACKENTRY = 0x1654AE6B, 0xAE
TRACKNUMBER, TRACKTYPE, FLAGDEFAULT, FLAGFORCED = 0xD7, 0x83, 0x88, 0x55AA
NAME, LANGUAGE, LANGUAGE_BCP47, CODECID, CODECPRIVATE = 0x536E, 0x22B59C, 0x22B59D, 0x86, 0x63A2
CONTENTENCODINGS, CONTENTENCODING, CONTENTCOMPRESSION = 0x6D80, 0x6240, 0x5034
CONTENTCOMPALGO, CONTENTCOMPSETTINGS, CONTENTENCRYPTION = 0x4254, 0x4255, 0x5035
CLUSTER, CLUSTER_TIMESTAMP, SIMPLEBLOCK, BLOCKGROUP, BLOCK, BLOCKDURATION = (
    0x1F43B675, 0xE7, 0xA3, 0xA0, 0xA1, 0x9B)
CUES, CHAPTERS, TAGS, ATTACHMENTS = 0x1C53BB6B, 0x1043A770, 0x1254C367, 0x1941A469
TOP_LEVEL = {SEEKHEAD, INFO, TRACKS, CLUSTER, CUES, CHAPTERS, TAGS, ATTACHMENTS}

TRACK_SUBTITLE = 0x11
UNKNOWN_SIZE = -1
MAX_SMALL_ELEMENT = 1 << 20         # 1 MB: limit for subtitle blocks, codec headers, etc.
MAX_TEXT_BYTES = 1 << 20            # limit after decompression
DEFAULT_CUE_MS = 4000
MAX_CUE_GAP_MS = 7000
CACHE_VERSION = 1
CACHE_KEEP = 40

TEXT_CODECS = {"S_TEXT/UTF8", "S_TEXT/ASCII", "S_TEXT/ASS", "S_TEXT/SSA", "S_ASS", "S_SSA",
               "S_TEXT/WEBVTT", "D_WEBVTT/SUBTITLES", "D_WEBVTT/CAPTIONS", "D_WEBVTT/DESCRIPTIONS"}
PICTURE_CODECS = {"S_HDMV/PGS", "S_VOBSUB", "S_DVBSUB", "S_IMAGE/BMP", "S_HDMV/TEXTST", "S_KATE"}


class NotMatroska(Exception):
    pass


@dataclass
class TrackInfo:
    number: int
    codec: str
    language: str = "en"     # Matroska's default language is English
    name: str = ""
    default: bool = False
    forced: bool = False
    codec_private: bytes = b""
    compression: Optional[int] = None   # 0 = zlib, 3 = header stripping
    strip_prefix: bytes = b""
    encrypted: bool = False
    styles: Dict[str, Tuple[bool, bool]] = field(default_factory=dict)

    @property
    def is_text(self) -> bool:
        return self.codec in TEXT_CODECS and not self.encrypted

    @property
    def is_ass(self) -> bool:
        return self.codec in ("S_TEXT/ASS", "S_TEXT/SSA", "S_ASS", "S_SSA")

    def describe(self) -> str:
        from .parsers import LANGUAGE_NAMES
        language = LANGUAGE_NAMES.get(self.language, self.language or "Unknown language")
        bits = [language]
        if self.name and self.name.lower() != language.lower():
            bits.append(self.name)
        if self.forced:
            bits.append("forced")
        if not self.is_text:
            bits.append("picture-based")
        return " · ".join(bits)


@dataclass
class FilmInfo:
    tracks: List[TrackInfo]
    timestamp_scale: int = 1_000_000  # nanoseconds per timestamp unit
    duration_ms: Optional[float] = None


class _Reader:
    """Minimal, defensive EBML reader over a binary file."""

    def __init__(self, handle: BinaryIO, size: int):
        self.f = handle
        self.size = size

    def tell(self) -> int:
        return self.f.tell()

    def seek(self, position: int) -> None:
        self.f.seek(position)

    def _byte(self) -> int:
        b = self.f.read(1)
        if not b:
            raise EOFError
        return b[0]

    def read_id(self) -> int:
        first = self._byte()
        length = 1
        mask = 0x80
        while length <= 4 and not first & mask:
            mask >>= 1
            length += 1
        if length > 4:
            raise ValueError("invalid element id")
        value = first
        for _ in range(length - 1):
            value = (value << 8) | self._byte()
        return value

    def read_vint(self) -> Tuple[int, int]:
        """Returns (value, length). An all-ones value means 'unknown size'."""
        first = self._byte()
        length = 1
        mask = 0x80
        while length <= 8 and not first & mask:
            mask >>= 1
            length += 1
        if length > 8:
            raise ValueError("invalid size")
        value = first & (mask - 1)
        all_ones = value == mask - 1
        for _ in range(length - 1):
            byte = self._byte()
            all_ones = all_ones and byte == 0xFF
            value = (value << 8) | byte
        return (UNKNOWN_SIZE if all_ones else value), length

    def read_header(self) -> Tuple[int, int, int]:
        """(id, size, data_start). Raises EOFError at the end of the file."""
        element_id = self.read_id()
        size, _ = self.read_vint()
        start = self.tell()
        if size != UNKNOWN_SIZE and start + size > self.size:
            size = max(0, self.size - start)  # truncated file: read what's there
        return element_id, size, start

    def read_bytes(self, size: int, limit: int = MAX_SMALL_ELEMENT) -> bytes:
        if size < 0 or size > limit:
            raise ValueError(f"element too large ({size} bytes)")
        data = self.f.read(size)
        if len(data) != size:
            raise EOFError
        return data

    def read_uint(self, size: int) -> int:
        return int.from_bytes(self.read_bytes(size, 8), "big") if size else 0

    def read_float(self, size: int) -> float:
        import struct
        data = self.read_bytes(size, 8)
        if size == 4:
            return struct.unpack(">f", data)[0]
        if size == 8:
            return struct.unpack(">d", data)[0]
        return 0.0

    def children(self, start: int, size: int):
        """Iterate (id, size, data_start) of the children of a known-size element."""
        end = start + size
        self.seek(start)
        while self.tell() < end:
            element_id, child_size, child_start = self.read_header()
            if child_size == UNKNOWN_SIZE:
                return
            yield element_id, child_size, child_start
            self.seek(child_start + child_size)


def _open_segment(reader: _Reader) -> Tuple[int, int]:
    """Check the EBML header and return (segment data start, segment end)."""
    try:
        element_id, size, start = reader.read_header()
    except (EOFError, ValueError) as exc:
        raise NotMatroska(str(exc)) from exc
    if element_id != EBML or size == UNKNOWN_SIZE:
        raise NotMatroska("no EBML header")
    doctype = ""
    for child_id, child_size, child_start in reader.children(start, size):
        if child_id == DOCTYPE:
            reader.seek(child_start)
            doctype = reader.read_bytes(child_size, 64).decode("ascii", "replace")
    if doctype not in ("matroska", "webm"):
        raise NotMatroska(f"doctype {doctype!r}")
    reader.seek(start + size)
    element_id, size, start = reader.read_header()
    if element_id != SEGMENT:
        raise NotMatroska("no segment")
    end = reader.size if size == UNKNOWN_SIZE else start + size
    return start, end


def _parse_tracks(reader: _Reader, start: int, size: int) -> List[TrackInfo]:
    tracks: List[TrackInfo] = []
    for entry_id, entry_size, entry_start in list(reader.children(start, size)):
        if entry_id != TRACKENTRY:
            continue
        values: Dict[int, object] = {}
        compression, strip_prefix, encrypted = None, b"", False
        for child_id, child_size, child_start in list(reader.children(entry_start, entry_size)):
            reader.seek(child_start)
            if child_id in (TRACKNUMBER, TRACKTYPE, FLAGDEFAULT, FLAGFORCED):
                values[child_id] = reader.read_uint(child_size)
            elif child_id in (NAME, LANGUAGE, LANGUAGE_BCP47, CODECID):
                values[child_id] = reader.read_bytes(child_size, 4096).decode("utf-8", "replace").rstrip("\x00")
            elif child_id == CODECPRIVATE and child_size <= MAX_SMALL_ELEMENT:
                values[child_id] = reader.read_bytes(child_size)
            elif child_id == CONTENTENCODINGS:
                compression, strip_prefix, encrypted = _parse_encodings(reader, child_start, child_size)
        if values.get(TRACKTYPE) != TRACK_SUBTITLE or not values.get(TRACKNUMBER):
            continue
        language = str(values.get(LANGUAGE_BCP47) or values.get(LANGUAGE) or "eng")
        info = TrackInfo(
            number=int(values[TRACKNUMBER]),
            codec=str(values.get(CODECID, "")),
            language=normalise_language(language),
            name=str(values.get(NAME, "")),
            default=bool(values.get(FLAGDEFAULT, 1)),
            forced=bool(values.get(FLAGFORCED, 0)),
            codec_private=bytes(values.get(CODECPRIVATE, b"")),
            compression=compression, strip_prefix=strip_prefix, encrypted=encrypted)
        if info.is_ass and info.codec_private:
            info.styles = parse_ass_styles(info.codec_private.decode("utf-8", "replace"))
        tracks.append(info)
    return tracks


def _parse_encodings(reader: _Reader, start: int, size: int):
    compression, prefix, encrypted = None, b"", False
    for enc_id, enc_size, enc_start in list(reader.children(start, size)):
        if enc_id != CONTENTENCODING:
            continue
        for child_id, child_size, child_start in list(reader.children(enc_start, enc_size)):
            if child_id == CONTENTENCRYPTION:
                encrypted = True
            elif child_id == CONTENTCOMPRESSION:
                algo = 0  # the spec's default algorithm is zlib
                for c_id, c_size, c_start in list(reader.children(child_start, child_size)):
                    reader.seek(c_start)
                    if c_id == CONTENTCOMPALGO:
                        algo = reader.read_uint(c_size)
                    elif c_id == CONTENTCOMPSETTINGS:
                        prefix = reader.read_bytes(c_size, 4096)
                compression = algo
    return compression, prefix, encrypted


def _scan_top_level(reader: _Reader, seg_start: int, seg_end: int):
    """Walk the segment's top-level elements until the first cluster."""
    info: Dict[str, object] = {"scale": 1_000_000, "duration": None, "tracks": None,
                               "seek": {}, "first_cluster": None}
    reader.seek(seg_start)
    while reader.tell() < seg_end:
        element_start = reader.tell()
        try:
            element_id, size, start = reader.read_header()
        except EOFError:
            break
        if element_id == CLUSTER:
            info["first_cluster"] = element_start
            break
        if size == UNKNOWN_SIZE:
            break
        if element_id == INFO:
            for child_id, child_size, child_start in list(reader.children(start, size)):
                reader.seek(child_start)
                if child_id == TIMESTAMPSCALE:
                    info["scale"] = reader.read_uint(child_size) or 1_000_000
                elif child_id == DURATION:
                    info["duration"] = reader.read_float(child_size)
        elif element_id == TRACKS:
            info["tracks"] = _parse_tracks(reader, start, size)
        elif element_id == SEEKHEAD:
            for seek_id, seek_size, seek_start in list(reader.children(start, size)):
                if seek_id != SEEK:
                    continue
                target, position = None, None
                for c_id, c_size, c_start in list(reader.children(seek_start, seek_size)):
                    reader.seek(c_start)
                    if c_id == SEEKID:
                        target = int.from_bytes(reader.read_bytes(c_size, 8), "big")
                    elif c_id == SEEKPOSITION:
                        position = reader.read_uint(c_size)
                if target is not None and position is not None:
                    info["seek"].setdefault(target, seg_start + position)  # type: ignore[union-attr]
        reader.seek(start + size)
    return info


def read_info(path: Path) -> FilmInfo:
    """Quickly read the subtitle track list (only looks at the start of the file)."""
    size = path.stat().st_size
    with open(path, "rb") as handle:
        reader = _Reader(handle, size)
        seg_start, seg_end = _open_segment(reader)
        info = _scan_top_level(reader, seg_start, seg_end)
        tracks = info["tracks"]
        if tracks is None and TRACKS in info["seek"]:  # Tracks stored after the clusters
            reader.seek(info["seek"][TRACKS])
            element_id, element_size, start = reader.read_header()
            if element_id == TRACKS and element_size != UNKNOWN_SIZE:
                tracks = _parse_tracks(reader, start, element_size)
        scale = int(info["scale"])
        duration = info["duration"]
        duration_ms = float(duration) * scale / 1e6 if duration else None
        return FilmInfo(tracks or [], scale, duration_ms)


# ------------------------------------------------------------------ extraction
Progress = Callable[[float, Optional[float]], None]     # (fraction 0..1, seconds left or None)
Deliver = Callable[[int, List[Cue]], None]               # (track number, new cues)


class _TrackState:
    def __init__(self, info: TrackInfo):
        self.info = info
        self.pending: Optional[Tuple[int, str, bool]] = None  # cue without a known end
        self.batch: List[Cue] = []

    def text_of(self, payload: bytes) -> Tuple[str, bool]:
        if self.info.compression == 0:
            inflater = zlib.decompressobj()
            payload = inflater.decompress(payload, MAX_TEXT_BYTES)
        elif self.info.compression == 3:
            payload = self.info.strip_prefix + payload
        text = payload.decode("utf-8", "replace").replace("\x00", "")
        if self.info.is_ass:
            fields = text.split(",", 8)
            if len(fields) < 9:
                return "", False
            bold, italic = self.info.styles.get(fields[2].strip().lstrip("*"), (False, False))
            return ass_to_markup(fields[8], italic=italic, bold=bold)
        return _plain_cue_text(text.split("\n"))

    def add(self, start: int, end: Optional[int], text: str, top: bool) -> None:
        if self.pending is not None:
            p_start, p_text, p_top = self.pending
            p_end = min(max(start, p_start + 1), p_start + MAX_CUE_GAP_MS)
            self.batch.append(Cue(p_start, p_end, p_text, p_top))
            self.pending = None
        if not text:
            return
        if end is None:
            self.pending = (start, text, top)
        else:
            self.batch.append(Cue(start, end, text, top))

    def finish(self) -> None:
        if self.pending is not None:
            p_start, p_text, p_top = self.pending
            self.batch.append(Cue(p_start, p_start + DEFAULT_CUE_MS, p_text, p_top))
            self.pending = None


def extract(path: Path, wanted: Sequence[TrackInfo], deliver: Deliver,
            progress: Optional[Progress] = None, cancel: Optional[threading.Event] = None,
            read_limit_mb: float = 0) -> bool:
    """Scan the whole film and deliver cues for the wanted text tracks as they're found.

    Returns True if the scan finished, False if it was cancelled.
    Raises SyncWatchError("SW-252") if the file is damaged.
    """
    states = {t.number: _TrackState(t) for t in wanted if t.is_text}
    if not states:
        return True
    size = path.stat().st_size
    started = time.monotonic()
    last_report = 0.0
    limit_bps = read_limit_mb * 1_000_000 if read_limit_mb > 0 else 0
    try:
        with open(path, "rb", buffering=1 << 20) as handle:
            reader = _Reader(handle, size)
            seg_start, seg_end = _open_segment(reader)
            info = _scan_top_level(reader, seg_start, seg_end)
            scale = int(info["scale"])
            position = info["first_cluster"]
            if position is None:
                return True  # no clusters at all
            reader.seek(position)
            while reader.tell() < seg_end:
                if cancel is not None and cancel.is_set():
                    return False
                try:
                    element_id, element_size, start = reader.read_header()
                except EOFError:
                    break
                if element_id == CLUSTER:
                    _read_cluster(reader, start, element_size, seg_end, scale, states)
                elif element_size == UNKNOWN_SIZE:
                    break
                else:
                    reader.seek(start + element_size)
                now = time.monotonic()
                if now - last_report > 0.25:
                    last_report = now
                    elapsed = now - started
                    for number, state in states.items():
                        if state.batch:
                            deliver(number, state.batch)
                            state.batch = []
                    if progress is not None:
                        fraction = min(1.0, reader.tell() / max(1, size))
                        eta = elapsed * (1 - fraction) / fraction if fraction > 0.02 else None
                        progress(fraction, eta)
                    if limit_bps:
                        expected = reader.tell() / limit_bps
                        if expected > elapsed:
                            time.sleep(min(1.0, expected - elapsed))
    except NotMatroska as exc:
        raise SyncWatchError("SW-252", f"not a Matroska file: {exc}") from exc
    except (OSError, ValueError) as exc:
        raise SyncWatchError("SW-252", f"{type(exc).__name__}: {exc}") from exc
    finally:
        for number, state in states.items():
            state.finish()
            if state.batch:
                deliver(number, state.batch)
                state.batch = []
    if progress is not None:
        progress(1.0, 0.0)
    return True


def _read_cluster(reader: _Reader, start: int, size: int, seg_end: int, scale: int,
                  states: Dict[int, _TrackState]) -> None:
    end = seg_end if size == UNKNOWN_SIZE else start + size
    cluster_time = 0
    reader.seek(start)
    while reader.tell() < end:
        element_start = reader.tell()
        try:
            element_id, element_size, data_start = reader.read_header()
        except EOFError:
            return
        if size == UNKNOWN_SIZE and element_id in TOP_LEVEL:
            reader.seek(element_start)  # next top-level element: hand back to the caller
            return
        if element_size == UNKNOWN_SIZE:
            return
        if element_id == CLUSTER_TIMESTAMP:
            reader.seek(data_start)
            cluster_time = reader.read_uint(element_size)
        elif element_id == SIMPLEBLOCK:
            _read_block(reader, data_start, element_size, None, cluster_time, scale, states)
        elif element_id == BLOCKGROUP:
            block, duration = None, None
            for child_id, child_size, child_start in list(reader.children(data_start, element_size)):
                if child_id == BLOCK:
                    block = (child_start, child_size)
                elif child_id == BLOCKDURATION:
                    reader.seek(child_start)
                    duration = reader.read_uint(child_size)
            if block is not None:
                _read_block(reader, block[0], block[1], duration, cluster_time, scale, states)
        reader.seek(data_start + element_size)


def _read_block(reader: _Reader, start: int, size: int, duration: Optional[int], cluster_time: int,
                scale: int, states: Dict[int, _TrackState]) -> None:
    reader.seek(start)
    track, length = reader.read_vint()
    state = states.get(track)
    if state is None:
        return  # video/audio/unwanted track: skip without reading the data
    header = reader.read_bytes(3, 3)
    relative = int.from_bytes(header[:2], "big", signed=True)
    flags = header[2]
    if (flags >> 1) & 0x3:
        log.debug("skipping laced subtitle block in track %d", track)
        return
    payload_size = size - length - 3
    if payload_size <= 0 or payload_size > MAX_SMALL_ELEMENT:
        return
    payload = reader.read_bytes(payload_size)
    start_ms = int((cluster_time + relative) * scale / 1_000_000)
    end_ms = int(start_ms + duration * scale / 1_000_000) if duration else None
    try:
        text, top = state.text_of(payload)
    except zlib.error:
        return
    state.add(max(0, start_ms), end_ms, text, top)


# ------------------------------------------------------------------ cache
def cache_key(path: Path) -> str:
    stat = path.stat()
    raw = f"{path.name}|{stat.st_size}|{stat.st_mtime_ns}|v{CACHE_VERSION}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def load_cache(cache_dir: Path, path: Path) -> Optional[Dict[int, List[Cue]]]:
    try:
        file = cache_dir / f"subs-{cache_key(path)}.json"
        raw = json.loads(file.read_text(encoding="utf-8"))
        if raw.get("version") != CACHE_VERSION:
            return None
        os.utime(file)  # mark as recently used
        return {int(number): [Cue(int(c[0]), int(c[1]), str(c[2]), bool(c[3])) for c in cues]
                for number, cues in raw["tracks"].items()}
    except (OSError, ValueError, KeyError, TypeError, IndexError):
        return None


def save_cache(cache_dir: Path, path: Path, tracks: Dict[int, List[Cue]]) -> None:
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        file = cache_dir / f"subs-{cache_key(path)}.json"
        data = {"version": CACHE_VERSION, "film": path.name,
                "tracks": {str(n): [[c.start, c.end, c.text, c.top] for c in cues]
                           for n, cues in tracks.items()}}
        tmp = file.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, file)
        old = sorted(cache_dir.glob("subs-*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        for stale in old[CACHE_KEEP:]:
            stale.unlink()
    except OSError as exc:
        log.info("could not write subtitle cache: %s", exc)
