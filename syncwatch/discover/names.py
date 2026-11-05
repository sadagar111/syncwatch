"""A film file's name -> the film's title and year (to look it up on TMDB, or to show it nicely).

    guess("The.Matrix.1999.1080p.BluRay.x264-GROUP.mkv")  -> Guess("The Matrix", 1999)
    guess("Inception (2010) {tmdb-27205}.mkv")             -> Guess("Inception", 2010, tmdb=27205)
    guess("Breaking.Bad.S01E01.720p.mkv")                  -> None (an episode, not a film)

No network and no guessing library: release names follow a few habits (the title, the year, then
quality tags such as 1080p, BluRay, x264 and the group's name), and that's what this looks for.
A wrong guess only means a wrong match or none (the film is then shown by its file name).
"""
from __future__ import annotations

import re
import time
import unicodedata
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class Guess:
    title: str
    year: int = 0       # 0: not in the name
    tmdb: int = 0       # a TMDB number written into the name ({tmdb-603}, [tmdbid-603]), else 0

    @property
    def label(self) -> str:
        return f"{self.title} ({self.year})" if self.year else self.title

    @property
    def key(self) -> str:
        """For spotting the same film under different names: 'matrix|1999'."""
        return f"{simplify(self.title)}|{self.year}"


_EXTENSION = re.compile(r"\.(?=[A-Za-z0-9]*[A-Za-z])[A-Za-z0-9]{2,4}$")
_TMDB_TAG = re.compile(r"[\[{(]\s*tmdb(?:id)?\s*[-=: ]\s*(\d{1,9})\s*[\]})]", re.IGNORECASE)
_OTHER_ID_TAG = re.compile(r"[\[{(]\s*(?:imdb|tvdb|tt)\w*\s*[-=: ]?[^\]})]*[\]})]", re.IGNORECASE)
_EPISODE = re.compile(r"(?i)(?:\bS\d{1,2}[ .]?E\d{1,3}\b|\b\d{1,2}x\d{2}\b|\bseason[ ._]?\d+|\bepisode[ ._]?\d+)")
_SITE = re.compile(r"(?i)^\s*(?:www\.)?[a-z0-9-]+\.(?:com|org|net|to|mx|me|io|cc|tv|xyz|se|nz|lol|in)\s*[-–]+\s*")
_BRACKETS = re.compile(r"\[[^\]]*\]|\{[^}]*\}")
_DOTTED_CODEC = re.compile(r"(?i)\b([xh])\.(26[45])\b")
_DOTTED_AUDIO = re.compile(r"(?i)\b(e?-?ddp?|e?-?ac-?3|aac|dts|truehd|atmos|opus|flac)(\d)\.(\d)\b")
_YEAR = re.compile(r"^(?:19|20)\d{2}$")
# Quality, source, codec and sound tags: the title has ended by the time one of these comes
_HARD_TAG = re.compile(
    r"(?i)^(?:\d{3,4}[pi]|[48]k|uhd|hdr(?:10)?\+?|dv|dovi|sdr|blu-?ray|bd(?:rip|remux)?|brrip|remux|"
    r"web-?(?:dl|rip)|webrip|hdtv|dvd(?:rip|scr|r|9|5)?|hd(?:rip|cam|ts|tc)|camrip|telesync|"
    r"[xh]26[45]|hevc|avc|xvid|divx|av1|vp9|e?-?ddp?\d*|e?-?ac-?3\d*|aac\d*|dts(?:-?hd)?\d*|truehd\d*|atmos|flac\d*|"
    r"opus\d*|mp3|10-?bit|8-?bit|nf|amzn|dsnp|hmax|atvp|pcok|hulu|yts|rarbg)$")
# Words that end a title but aren't part of it ("Extended Edition", "Director's Cut")
_SOFT = {"extended", "unrated", "uncut", "remastered", "theatrical", "edition", "directors", "director's", "dc",
         "imax", "proper", "repack", "limited", "internal", "multi", "dual", "dubbed", "subbed", "complete"}


def simplify(text: str) -> str:
    """'Amélie: The Movie!' -> 'amelie the movie' (lower case, no accents or punctuation)."""
    decomposed = unicodedata.normalize("NFKD", text)
    plain = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    words = re.sub(r"[^\w]+", " ", plain.lower().replace("&", " and ")).split()
    return " ".join(words)


def _is_hard_tag(token: str) -> bool:
    return bool(_HARD_TAG.match(token) or _HARD_TAG.match(token.split("-")[0]))


def guess(file_name: str) -> Optional[Guess]:
    """The film's title (and year) from its file name; None for a show's episode or nothing usable."""
    name = file_name.replace("\\", "/").rsplit("/", 1)[-1]
    name = _EXTENSION.sub("", name.strip())
    if _EPISODE.search(name):
        return None
    tmdb_match = _TMDB_TAG.search(name)
    tmdb = int(tmdb_match.group(1)) if tmdb_match else 0
    name = _TMDB_TAG.sub(" ", name)
    name = _OTHER_ID_TAG.sub(" ", name)
    name = _SITE.sub("", name)
    name = _BRACKETS.sub(" ", name)
    name = _DOTTED_CODEC.sub(r"\g<1>\g<2>", name)
    name = _DOTTED_AUDIO.sub(r"\g<1>\g<2>\g<3>", name)
    name = re.sub(r"[()]", " ", name)
    name = re.sub(r"[._]+", " ", name)
    tokens = [token for token in name.split() if re.search(r"\w", token)]
    if not tokens:
        return None
    hard = next((i for i, token in enumerate(tokens) if i > 0 and _is_hard_tag(token)), len(tokens))
    newest = time.localtime().tm_year + 2
    years = [i for i in range(1, hard) if _YEAR.match(tokens[i]) and 1900 <= int(tokens[i]) <= newest]
    if years:
        title_tokens, year = tokens[:years[-1]], int(tokens[years[-1]])
    else:
        title_tokens, year = tokens[:hard], 0
    while len(title_tokens) > 1 and (title_tokens[-1].lower() in _SOFT or (
            title_tokens[-1].lower() == "cut" and len(title_tokens) > 2 and title_tokens[-2].lower() in _SOFT)):
        title_tokens.pop()
    title = " ".join(title_tokens).strip(" -–")
    if not title:
        return None
    if title == title.lower():
        title = title.title()
    return Guess(title[:200], year, tmdb)
