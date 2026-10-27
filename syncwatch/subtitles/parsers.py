"""Reading subtitle files: .srt, .vtt, .ass and .ssa.

Downloaded subtitle files are messy: missing blank lines, wrong numbering,
odd encodings. These parsers are deliberately forgiving, skipping what they
can't understand instead of failing.

Encodings: Slovak and Czech subtitles are often not UTF-8 but Windows-1250 or
ISO-8859-2, and French/Spanish ones Windows-1252. ``decode`` picks the most
likely one by scoring which reading produces real letters (č, ř, ľ, è, ñ …)
instead of symbols (©, », µ …). A language tag in the file name
(``film.sk.srt``) or your preferred languages decides close calls.
"""
from __future__ import annotations

import html
import re
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from ..errors import SyncWatchError
from .model import Cue, SubtitleTrack

MAX_FILE_BYTES = 10 * 1024 * 1024
SUBTITLE_EXTENSIONS = (".srt", ".vtt", ".ass", ".ssa")

# ---------------------------------------------------------------- languages
_LANGUAGE_WORDS: Dict[str, Sequence[str]] = {
    "sk": ("sk", "slk", "slo", "slovak", "slovensky", "slovencina"),
    "cs": ("cs", "cz", "cze", "ces", "czech", "cesky", "cestina"),
    "en": ("en", "eng", "english"),
    "de": ("de", "ger", "deu", "german", "deutsch"),
    "hu": ("hu", "hun", "hungarian", "magyar"),
    "pl": ("pl", "pol", "polish", "polski"),
    "fr": ("fr", "fre", "fra", "french"),
    "es": ("es", "spa", "spanish", "espanol"),
    "it": ("it", "ita", "italian"),
    "pt": ("pt", "por", "portuguese"),
    "nl": ("nl", "dut", "nld", "dutch"),
    "ru": ("ru", "rus", "russian"),
    "uk": ("uk", "ukr", "ukrainian"),
    "hr": ("hr", "hrv", "croatian"),
    "sl": ("sl", "slv", "slovenian"),
    "ro": ("ro", "rum", "ron", "romanian"),
}
_WORD_TO_LANG = {w: code for code, words in _LANGUAGE_WORDS.items() for w in words}
LANGUAGE_NAMES = {"sk": "Slovak", "cs": "Czech", "en": "English", "de": "German", "hu": "Hungarian",
                  "pl": "Polish", "fr": "French", "es": "Spanish", "it": "Italian", "pt": "Portuguese",
                  "nl": "Dutch", "ru": "Russian", "uk": "Ukrainian", "hr": "Croatian",
                  "sl": "Slovenian", "ro": "Romanian"}
_CENTRAL_EUROPEAN = {"sk", "cs", "pl", "hu", "hr", "sl", "ro"}

# Three-letter codes used inside MKV files map onto the same two-letter codes.
ISO639_2 = {"slk": "sk", "slo": "sk", "ces": "cs", "cze": "cs", "eng": "en", "deu": "de",
            "ger": "de", "hun": "hu", "pol": "pl", "fra": "fr", "fre": "fr", "spa": "es",
            "ita": "it", "por": "pt", "nld": "nl", "dut": "nl", "rus": "ru", "ukr": "uk",
            "hrv": "hr", "slv": "sl", "ron": "ro", "rum": "ro"}


def normalise_language(code: str) -> str:
    """'slk' / 'sk-SK' / 'Slovak' -> 'sk'. Unknown codes come back lowercased."""
    code = (code or "").strip().lower().replace("_", "-")
    base = code.split("-")[0]
    return ISO639_2.get(base) or _WORD_TO_LANG.get(base) or base


def language_from_name(path: Path, film_stem: str = "") -> str:
    """Guess the language from tokens in a subtitle file name, e.g. 'Film.2021.sk.srt'."""
    stem = path.stem
    if film_stem and stem.lower().startswith(film_stem.lower()):
        stem = stem[len(film_stem):]
    for token in reversed(re.split(r"[.\s_\-\[\]()]+", stem.lower())):
        if token in _WORD_TO_LANG:
            return _WORD_TO_LANG[token]
    return ""


# ---------------------------------------------------------------- encodings
_SKCZ = set("áäčďéěíĺľňóôŕřšťúůýžÁÄČĎÉĚÍĹĽŇÓÔŔŘŠŤÚŮÝŽ")
_OTHER_CE = set("ąęłńśźżőűĄĘŁŃŚŹŻŐŰ")
_WESTERN = set("àâæçèéêëîïôœùûüÿñãõåøäöüßáíóúÀÂÆÇÈÉÊËÎÏÔŒÙÛÜŸÑÃÕÅØÄÖÜÁÍÓÚ¿¡")
_SUSPICIOUS = set("¤¦§¨©ª«¬®¯°±²³´µ¶·¸¹º»¼½¾×÷˘˙˛˝ˇ")


def _score(text: str, central_european: bool) -> float:
    score = 0.0
    for ch in text:
        if ch < "\x80":
            continue
        if "\x80" <= ch <= "\x9f" or ch == "\ufffd":
            score -= 6
        elif ch in _SUSPICIOUS:
            score -= 4
        elif central_european:
            score += 3 if ch in _SKCZ else 1 if (ch in _OTHER_CE or ch in _WESTERN) else 0
        else:
            score += 3 if ch in _WESTERN else 0.5 if (ch in _SKCZ or ch in _OTHER_CE) else 0
    return score


def decode(data: bytes, forced: str = "auto", language_hint: str = "",
           preferred_languages: Sequence[str] = ()) -> Tuple[str, str]:
    """Bytes -> (text, encoding used)."""
    if forced and forced != "auto":
        return data.decode(forced, errors="replace"), forced
    for bom, encoding in ((b"\xef\xbb\xbf", "utf-8-sig"), (b"\xff\xfe", "utf-16"), (b"\xfe\xff", "utf-16")):
        if data.startswith(bom):
            return data.decode(encoding, errors="replace"), encoding
    sample = data[:4000]
    if len(sample) >= 4 and sample.count(b"\x00") > len(sample) // 4:
        encoding = "utf-16-le" if sample[1:2] == b"\x00" else "utf-16-be"
        return data.decode(encoding, errors="replace"), encoding
    try:
        return data.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        pass
    hint = normalise_language(language_hint)
    if hint:
        central = hint in _CENTRAL_EUROPEAN
    else:
        prefs = [normalise_language(p) for p in preferred_languages]
        central = any(p in _CENTRAL_EUROPEAN for p in prefs) or not prefs
    candidates = ("cp1250", "iso-8859-2", "cp1252") if central else ("cp1252", "cp1250", "iso-8859-2")
    best_text, best_encoding, best_score = "", candidates[0], float("-inf")
    for encoding in candidates:
        text = data.decode(encoding, errors="replace")
        score = _score(text, central)
        if score > best_score:  # strict '>' keeps the earlier, preferred candidate on ties
            best_text, best_encoding, best_score = text, encoding, score
    return best_text, best_encoding


# ---------------------------------------------------------------- shared text cleanup
_KEEP_TAG = re.compile(r"</?([ibu])\s*>", re.IGNORECASE)
_ANY_TAG = re.compile(r"<[^>\n]{0,200}>")
_ASS_BLOCK = re.compile(r"\{([^}]*)\}")
_ASS_TAG = re.compile(r"\\(i|b|u|an|a|p)(-?\d+)")


def _normalise_html_like(text: str) -> str:
    """Keep only <i>, <b>, <u> (lowercased); drop every other tag but keep its text.

    The tags we keep are swapped for placeholders first, so removing "all other
    tags" can't touch them, then put back.
    """
    kept: List[str] = []

    def park(match: "re.Match[str]") -> str:
        closing = match.group(0)[1] == "/"
        tag = match.group(1).lower()
        kept.append(f"</{tag}>" if closing else f"<{tag}>")
        return f"\x01{len(kept) - 1}\x02"

    text = _KEEP_TAG.sub(park, text)
    text = _ANY_TAG.sub("", text)
    return re.sub(r"\x01(\d+)\x02", lambda m: kept[int(m.group(1))], text)


def ass_to_markup(text: str, italic: bool = False, bold: bool = False) -> Tuple[str, bool]:
    """Convert ASS dialogue text (with {\\override} tags) to mini markup.

    Returns (markup, top) where ``top`` means the line asked to be shown at the
    top of the screen. Drawing commands (\\p1) are dropped; they're shapes, not text.
    """
    top = False
    out: List[str] = []
    state = {"i": False, "b": False, "u": False}
    drawing = False

    def set_style(tag: str, on: bool) -> None:
        if state[tag] != on:
            out.append(f"<{tag}>" if on else f"</{tag}>")
            state[tag] = on

    set_style("i", italic)
    set_style("b", bold)
    position = 0
    for match in _ASS_BLOCK.finditer(text):
        if not drawing:
            out.append(text[position:match.start()])
        for name, value in _ASS_TAG.findall(match.group(1)):
            number = int(value)
            if name in ("i", "u"):
                set_style(name, number != 0)
            elif name == "b":
                set_style("b", number != 0)
            elif name == "an":
                top = number in (7, 8, 9)
            elif name == "a":  # old SSA alignment: 5-7 and 9-11 are top-ish
                top = number in (5, 6, 7, 9, 10, 11)
            elif name == "p":
                drawing = number != 0
        position = match.end()
    if not drawing:
        out.append(text[position:])
    for tag in ("u", "b", "i"):
        set_style(tag, False)
    markup = "".join(out).replace("\\N", "\n").replace("\\n", " ").replace("\\h", "\u00a0")
    return _clean_markup(markup), top


def _clean_markup(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = [ln.strip() for ln in text.split("\n")]
    text = "\n".join(ln for ln in lines if _visible(ln))
    for tag in ("i", "b", "u"):  # drop empty pairs like <i></i>
        text = text.replace(f"<{tag}></{tag}>", "")
    return text.strip()


def _visible(line: str) -> bool:
    return bool(re.sub(r"</?[ibu]>", "", line).strip())


def _plain_cue_text(lines: List[str]) -> Tuple[str, bool]:
    """SRT/VTT cue text: may contain HTML-like tags and stray ASS {\\an8} tags."""
    text = "\n".join(lines)
    top = False
    if "{" in text:
        converted, top = ass_to_markup(text)
        text = converted
    text = _normalise_html_like(text)
    return _clean_markup(html.unescape(text)), top


# ---------------------------------------------------------------- SRT
_SRT_TIME = re.compile(
    r"(\d{1,3}):(\d{1,2}):(\d{1,2})(?:[,.:](\d{1,3}))?\s*-{1,3}>\s*"
    r"(\d{1,3}):(\d{1,2}):(\d{1,2})(?:[,.:](\d{1,3}))?")


def _ms(h: str, m: str, s: str, frac: Optional[str]) -> int:
    millis = int((frac or "0").ljust(3, "0")[:3])
    return ((int(h) * 60 + int(m)) * 60 + int(s)) * 1000 + millis


def parse_srt(text: str) -> List[Cue]:
    cues: List[Cue] = []
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    index = 0
    total = len(lines)
    while index < total:
        match = _SRT_TIME.search(lines[index])
        if not match:
            index += 1
            continue
        start = _ms(*match.group(1, 2, 3, 4))
        end = _ms(*match.group(5, 6, 7, 8))
        index += 1
        body: List[str] = []
        while index < total and lines[index].strip():
            # A cue without a blank line before the next one: "12" followed by a time line.
            if (lines[index].strip().isdigit() and index + 1 < total
                    and _SRT_TIME.search(lines[index + 1])):
                break
            if _SRT_TIME.search(lines[index]):
                break
            body.append(lines[index])
            index += 1
        text_markup, top = _plain_cue_text(body)
        if text_markup and end > start:
            cues.append(Cue(start, end, text_markup, top))
    return cues


# ---------------------------------------------------------------- WebVTT
_VTT_TIME = re.compile(
    r"(?:(\d{1,3}):)?(\d{1,2}):(\d{1,2})[.,](\d{1,3})\s*-->\s*"
    r"(?:(\d{1,3}):)?(\d{1,2}):(\d{1,2})[.,](\d{1,3})(.*)")
_VTT_TIMESTAMP_TAG = re.compile(r"<\d{1,3}:\d{2}(?::\d{2})?[.,]\d{1,3}>")


def parse_vtt(text: str) -> List[Cue]:
    cues: List[Cue] = []
    blocks = re.split(r"\n\s*\n", text.replace("\r\n", "\n").replace("\r", "\n"))
    for block in blocks:
        lines = [ln for ln in block.split("\n")]
        for position, line in enumerate(lines):
            match = _VTT_TIME.search(line)
            if not match:
                continue
            g = match.groups()
            start = _ms(g[0] or "0", g[1], g[2], g[3])
            end = _ms(g[4] or "0", g[5], g[6], g[7])
            settings = g[8] or ""
            top = bool(re.search(r"line:\s*(0|1|2|[0-9]%|1[0-9]%|20%)(\s|,|$)", settings))
            body = [_VTT_TIMESTAMP_TAG.sub("", ln) for ln in lines[position + 1:]]
            markup, ass_top = _plain_cue_text(body)
            if markup and end > start:
                cues.append(Cue(start, end, markup, top or ass_top))
            break
    return cues


# ---------------------------------------------------------------- ASS / SSA
def _ass_time(value: str) -> int:
    match = re.match(r"\s*(\d+):(\d{1,2}):(\d{1,2})[.,](\d{1,3})", value)
    if not match:
        raise ValueError(value)
    h, m, s, frac = match.groups()
    return ((int(h) * 60 + int(m)) * 60 + int(s)) * 1000 + int(frac.ljust(3, "0")[:3])


def parse_ass_styles(header: str) -> Dict[str, Tuple[bool, bool]]:
    """Style name -> (bold, italic) from the [V4+ Styles] / [V4 Styles] section."""
    styles: Dict[str, Tuple[bool, bool]] = {}
    fields: List[str] = []
    in_styles = False
    for line in header.replace("\r", "").split("\n"):
        stripped = line.strip()
        if stripped.startswith("["):
            in_styles = "styles" in stripped.lower()
            continue
        if not in_styles or ":" not in stripped:
            continue
        kind, _, rest = stripped.partition(":")
        values = [v.strip() for v in rest.split(",")]
        if kind.strip().lower() == "format":
            fields = [v.lower() for v in values]
        elif kind.strip().lower() == "style" and fields:
            row = dict(zip(fields, values))
            bold = row.get("bold", "0") not in ("0", "")
            italic = row.get("italic", "0") not in ("0", "")
            styles[row.get("name", "")] = (bold, italic)
    return styles


def parse_ass(text: str) -> List[Cue]:
    cues: List[Cue] = []
    styles = parse_ass_styles(text)
    fields: List[str] = []
    in_events = False
    for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        stripped = line.strip()
        if stripped.startswith("["):
            in_events = stripped.lower() == "[events]"
            continue
        if not in_events or ":" not in stripped:
            continue
        kind, _, rest = stripped.partition(":")
        kind = kind.strip().lower()
        if kind == "format":
            fields = [v.strip().lower() for v in rest.split(",")]
            continue
        if kind != "dialogue" or not fields:
            continue
        values = rest.split(",", len(fields) - 1)
        if len(values) < len(fields):
            continue
        row = dict(zip(fields, (v.strip() if f != "text" else v for f, v in zip(fields, values))))
        try:
            start, end = _ass_time(row.get("start", "")), _ass_time(row.get("end", ""))
        except ValueError:
            continue
        bold, italic = styles.get(row.get("style", "").lstrip("*"), (False, False))
        markup, top = ass_to_markup(row.get("text", ""), italic=italic, bold=bold)
        if markup and end > start:
            cues.append(Cue(start, end, markup, top))
    cues.sort(key=lambda c: c.start)
    return cues


# ---------------------------------------------------------------- files
def detect_format(path: Path, text: str) -> str:
    suffix = path.suffix.lower()
    head = text.lstrip("\ufeff \t\r\n")[:300]
    if head.upper().startswith("WEBVTT"):
        return "vtt"
    if "[script info]" in head.lower() or suffix in (".ass", ".ssa"):
        return "ass"
    if suffix == ".vtt":
        return "vtt"
    return "srt"


def load_file(path: Path, forced_encoding: str = "auto",
              preferred_languages: Sequence[str] = (), film_stem: str = "") -> SubtitleTrack:
    """Read a subtitle file into a track. Raises SyncWatchError (SW-250 / SW-251)."""
    try:
        size = path.stat().st_size
        if size > MAX_FILE_BYTES:
            raise SyncWatchError("SW-250", f"file is {size / 1e6:.1f} MB", limit_mb=MAX_FILE_BYTES // 1_000_000)
        data = path.read_bytes()
    except OSError as exc:
        raise SyncWatchError("SW-250", f"{type(exc).__name__}: {exc}",
                             limit_mb=MAX_FILE_BYTES // 1_000_000) from exc
    language = language_from_name(path, film_stem)
    text, encoding = decode(data, forced_encoding, language, preferred_languages)
    kind = detect_format(path, text)
    parser = {"srt": parse_srt, "vtt": parse_vtt, "ass": parse_ass}[kind]
    cues = parser(text)
    if not cues and kind != "srt":
        cues = parse_srt(text)  # misnamed file? try the most common format too
    if not cues:
        raise SyncWatchError("SW-251", f"no cues found ({kind}, {encoding}, {len(data)} bytes)")
    label = path.name
    if language:
        label = f"{LANGUAGE_NAMES.get(language, language)} ({path.name})"
    track = SubtitleTrack(cues, label=label, language=language, source=f"file:{path}")
    track.encoding = encoding  # type: ignore[attr-defined]
    return track


def find_external(film: Path) -> List[Tuple[Path, str]]:
    """Subtitle files that belong to a film: same name (plus any tag), or in a Subs folder.

    Returns (path, language) pairs; language may be "".
    """
    found: List[Tuple[Path, str]] = []
    stem = film.stem.lower()
    folder = film.parent
    try:
        candidates = list(folder.iterdir())
    except OSError:
        return found
    for path in candidates:
        if path.suffix.lower() in SUBTITLE_EXTENSIONS and path.is_file() and path.stem.lower().startswith(stem):
            found.append((path, language_from_name(path, film.stem)))
    for sub_folder in ("subs", "subtitles", "sub"):
        for entry in candidates:
            if entry.is_dir() and entry.name.lower() == sub_folder:
                try:
                    for path in sorted(entry.iterdir()):
                        if path.suffix.lower() in SUBTITLE_EXTENSIONS and path.is_file():
                            found.append((path, language_from_name(path)))
                except OSError:
                    pass
    return found


def pick_preferred(options: Sequence[Tuple[object, str]], preferred: Sequence[str]) -> Optional[object]:
    """From (thing, language) pairs pick the best according to the preference list."""
    ranked = [normalise_language(p) for p in preferred]
    best, best_rank = None, len(ranked) + 1
    for thing, language in options:
        lang = normalise_language(language)
        rank = ranked.index(lang) if lang in ranked else len(ranked)
        if rank < best_rank:
            best, best_rank = thing, rank
    return best if best_rank < len(ranked) else None
