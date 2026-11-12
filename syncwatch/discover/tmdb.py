"""The Movie Database (TMDB): finding films, their details, recommendations and what's trending.

TMDB is free for personal, non-commercial use with a key: themoviedb.org > Settings > API (the
long "API Read Access Token" or the short "API Key", both work). Without a key of one's own,
SyncWatch's relay asks TMDB with its key (since 0.5.1: relay/src/tmdb.js, only these questions).
A key in SyncWatch's settings (Settings > Ideas) is used instead and only ever sent to
api.themoviedb.org: it's never logged, never put into error reports and never part of the cache.

Everything TMDB answers is checked (types, lengths, ranges, what an image path may look like) and
only the checked parts are kept, in a small cache on disk, so the same film isn't asked twice and
the lists open at once: searches for 30 days, a film's details and recommendations for 7 days,
trending for 12 hours. Posters come from image.tmdb.org and are kept as files (the oldest go
once there are more than 400). Reading the cache checks everything again.

No Qt here: ui/discover_ui.py runs this in a background thread.

TMDB's terms ask for this notice, shown in the Discover window: "This product uses the TMDB API
but is not endorsed or certified by TMDB." Where-to-watch data comes from JustWatch (via TMDB).
"""
from __future__ import annotations

import http.client
import json
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

log = logging.getLogger("syncwatch.tmdb")

API = "https://api.themoviedb.org/3"
IMAGES = "https://image.tmdb.org/t/p/"
POSTER_SIZE = "w185"
NOTICE = "This product uses the TMDB API but is not endorsed or certified by TMDB."
MAX_ANSWER = 3_000_000      # bytes of JSON (TMDB's biggest answers used here are about 100 KB)
MAX_IMAGE = 2_000_000
TIMEOUT_S = 12.0
GAP_S = 0.06                # at most ~16 questions a second (TMDB allows far more; be polite)
CACHE_ENTRIES = 800
POSTER_FILES = 400
SEARCH_RESULTS = 5          # a search keeps its best few results (the first one is nearly always it)
LIST_OVERVIEW = 300         # films in lists keep a short description; a film's own page the long one
DAY = 86400.0
AGES = {"search": 30 * DAY, "movie": 7 * DAY, "trending": 0.5 * DAY}

GENRES = {28: "Action", 12: "Adventure", 16: "Animation", 35: "Comedy", 80: "Crime", 99: "Documentary",
          18: "Drama", 10751: "Family", 14: "Fantasy", 36: "History", 27: "Horror", 10402: "Music",
          9648: "Mystery", 10749: "Romance", 878: "Science Fiction", 10770: "TV Movie", 53: "Thriller",
          10752: "War", 37: "Western"}

IMAGE_PATH = re.compile(r"^/[A-Za-z0-9_-]{1,80}\.(?:jpg|jpeg|png|webp)$")
WATCH_LINK = re.compile(r"^https://www\.themoviedb\.org/[A-Za-z0-9/_.?=&%-]{1,400}$")
_DATE = re.compile(r"^(\d{4})-\d{2}-\d{2}$")
_VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
_REGION = re.compile(r"^[A-Z]{2}$")
_LANGUAGE = re.compile(r"^[a-z]{2}(?:-[A-Z]{2})?$")
_TOKEN = re.compile(r"^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$")   # the Read Access Token (a JWT)
_KEY = re.compile(r"^[0-9a-f]{32}$")                                    # the API Key
_IMAGE_STARTS = (b"\xff\xd8\xff", b"\x89PNG\r\n\x1a\n", b"RIFF")


class TmdbError(Exception):
    """kind: nokey (none set), key (TMDB didn't accept it), relay (the relay can't ask TMDB: it has
    no key, TMDB refused it, or it's an older relay), network (no answer), busy (TMDB or the relay
    is overloaded or limiting), missing (no such film), odd (an answer that didn't make sense)."""

    def __init__(self, kind: str, detail: str = ""):
        super().__init__(f"{kind}: {detail}" if detail else kind)
        self.kind = kind
        self.detail = detail


@dataclass(frozen=True)
class Movie:
    id: int
    title: str
    year: int = 0
    poster: str = ""            # TMDB image path ("/abc.jpg"), "" if none
    overview: str = ""
    rating: float = 0.0         # 0-10, from TMDB's users
    votes: int = 0
    popularity: float = 0.0
    genres: Tuple[str, ...] = ()
    original_title: str = ""

    @property
    def label(self) -> str:
        return f"{self.title} ({self.year})" if self.year else self.title


@dataclass(frozen=True)
class Providers:
    """Where a film can be watched in one country (JustWatch's data, through TMDB)."""
    region: str
    link: str                   # TMDB's page listing them (opens in the browser)
    stream: Tuple[str, ...] = ()     # in a subscription (or free)
    rent: Tuple[str, ...] = ()
    buy: Tuple[str, ...] = ()

    def summary(self) -> str:
        parts = []
        if self.stream:
            parts.append("Streaming on " + ", ".join(self.stream[:5]))
        if self.rent:
            parts.append("to rent on " + ", ".join(self.rent[:4]))
        elif self.buy:
            parts.append("to buy on " + ", ".join(self.buy[:4]))
        text = "; ".join(parts)
        return text[:1].upper() + text[1:]


@dataclass(frozen=True)
class Details:
    movie: Movie
    runtime: int = 0                        # minutes
    tagline: str = ""
    trailer: str = ""                       # a YouTube video ID
    providers: Optional[Providers] = None
    recommendations: Tuple[Movie, ...] = ()


def key_kind(key: str) -> str:
    """'key' (the 32-character API Key), 'token' (the long Read Access Token) or '' (neither)."""
    key = (key or "").strip()
    if _KEY.match(key):
        return "key"
    if 60 <= len(key) <= 1500 and _TOKEN.match(key):
        return "token"
    return ""


# ---------------------------------------------------------------- checking TMDB's answers
def _text(value: Any, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    clean = "".join(ch for ch in value if ch >= " " or ch in "\n\t")
    return " ".join(clean.split())[:limit]


def _number(value: Any, low: float, high: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value:
        return 0.0
    return float(min(high, max(low, value)))


def _whole(value: Any, low: int, high: int) -> int:
    return int(_number(value, low, high))


def _movie_id(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and 0 < value < 1 << 31 else 0


def clean_movie(raw: Any, overview_limit: int = 1500) -> Optional[Dict[str, Any]]:
    """One film from TMDB (or from the cache) -> its checked fields, None if unusable (or adult)."""
    if not isinstance(raw, dict) or raw.get("adult") is True:
        return None
    movie_id, title = _movie_id(raw.get("id")), _text(raw.get("title"), 200)
    if not movie_id or not title:
        return None
    date = raw.get("release_date")
    match = _DATE.match(date) if isinstance(date, str) else None
    poster = raw.get("poster_path")
    genres: List[str] = []
    for entry in raw.get("genres") or []:           # a film's own page: [{"id": 28, "name": "Action"}]
        name = entry.get("name") if isinstance(entry, dict) else entry   # (the cache keeps the names)
        if isinstance(name, str) and name.strip() and len(genres) < 6:
            genres.append(_text(name, 40))
    if not genres:                                  # lists: genre numbers
        for number in raw.get("genre_ids") or []:
            if number in GENRES and len(genres) < 6 and not isinstance(number, bool):
                genres.append(GENRES[number])
    return {"id": movie_id, "title": title, "release_date": match.group(0) if match else "",
            "poster_path": poster if isinstance(poster, str) and IMAGE_PATH.match(poster) else "",
            "overview": _text(raw.get("overview"), overview_limit),
            "vote_average": round(_number(raw.get("vote_average"), 0, 10), 1),
            "vote_count": _whole(raw.get("vote_count"), 0, 1 << 30),
            "popularity": round(_number(raw.get("popularity"), 0, 1e7), 2),
            "genres": genres, "original_title": _text(raw.get("original_title"), 200)}


def to_movie(raw: Any) -> Optional[Movie]:
    clean = clean_movie(raw)
    if clean is None:
        return None
    date = clean["release_date"]
    return Movie(clean["id"], clean["title"], int(date[:4]) if date else 0, clean["poster_path"],
                 clean["overview"], clean["vote_average"], clean["vote_count"], clean["popularity"],
                 tuple(clean["genres"]), clean["original_title"] if clean["original_title"] != clean["title"] else "")


def clean_list(raw: Any, limit: int = 20) -> List[Dict[str, Any]]:
    films, seen = [], set()
    for entry in raw if isinstance(raw, list) else []:
        clean = clean_movie(entry, LIST_OVERVIEW)
        if clean is not None and clean["id"] not in seen:
            seen.add(clean["id"])
            films.append(clean)
            if len(films) >= limit:
                break
    return films


def movies(raw: Any) -> List[Movie]:
    found = [to_movie(entry) for entry in (raw if isinstance(raw, list) else [])]
    return [movie for movie in found if movie is not None]


def pick_trailer(raw: Any, language: str = "en") -> str:
    """The best YouTube trailer among a film's videos: official trailers in the language first."""
    best, best_rank = "", None
    for video in raw if isinstance(raw, list) else []:
        if not isinstance(video, dict) or video.get("site") != "YouTube":
            continue
        key = video.get("key")
        if not isinstance(key, str) or not _VIDEO_ID.match(key):
            continue
        kind = video.get("type")
        rank = ({"Trailer": 0, "Teaser": 2}.get(kind, 4) + (0 if video.get("official") is True else 1)
                + (0 if video.get("iso_639_1") in (language, None) else 3))
        if best_rank is None or rank < best_rank:
            best, best_rank = key, rank
    return best


def _names(raw: Any) -> List[str]:
    names = []
    for entry in raw if isinstance(raw, list) else []:
        name = entry.get("provider_name") if isinstance(entry, dict) else entry
        name = _text(name, 60)
        if name and name not in names and len(names) < 8:
            names.append(name)
    return names


def clean_providers(raw: Any, region: str) -> Optional[Dict[str, Any]]:
    """TMDB's "watch/providers" (all countries) -> this country's, or None."""
    if not isinstance(raw, dict):
        return None
    if "region" in raw:                                     # already checked once (from the cache)
        here, region, streaming = raw, raw.get("region"), raw.get("stream")
    else:
        results = raw.get("results")
        here = results.get(region) if isinstance(results, dict) else None
        streaming = [entry for name in ("flatrate", "free", "ads") if isinstance(here, dict)
                     for entry in (here.get(name) if isinstance(here.get(name), list) else [])]
    if not isinstance(here, dict) or not isinstance(region, str) or not _REGION.match(region):
        return None
    link = here.get("link")
    if not isinstance(link, str) or not WATCH_LINK.match(link):
        return None
    return {"region": region, "link": link, "stream": _names(streaming), "rent": _names(here.get("rent")),
            "buy": _names(here.get("buy"))}


def clean_details(raw: Any, region: str, language: str) -> Dict[str, Any]:
    film = clean_movie(raw)
    if film is None:
        raise TmdbError("odd", "a film without a number or title")
    videos = raw.get("videos")
    trailer = raw.get("trailer") if isinstance(raw.get("trailer"), str) else pick_trailer(
        videos.get("results") if isinstance(videos, dict) else None, language.split("-")[0])
    recommendations = raw.get("recommendations")
    providers = raw.get("providers") if "providers" in raw else raw.get("watch/providers")
    film.update({"runtime": _whole(raw.get("runtime"), 0, 2000), "tagline": _text(raw.get("tagline"), 300),
                 "trailer": trailer if _VIDEO_ID.match(trailer or "") else "",
                 "providers": clean_providers(providers, region) if region else None,
                 "recommendations": {"results": clean_list(recommendations.get("results")
                                                           if isinstance(recommendations, dict) else None)}})
    return film


def to_details(raw: Any, region: str, language: str) -> Details:
    clean = clean_details(raw, region, language)
    movie = to_movie(clean)
    assert movie is not None
    providers = clean["providers"]
    return Details(movie, clean["runtime"], clean["tagline"], clean["trailer"],
                   Providers(providers["region"], providers["link"], tuple(providers["stream"]),
                             tuple(providers["rent"]), tuple(providers["buy"])) if providers else None,
                   tuple(movies(clean["recommendations"]["results"])))


# ---------------------------------------------------------------- the cache
class Cache:
    """Checked answers by question, with the time they came; one JSON file, written atomically."""

    def __init__(self, folder: Optional[Path]):
        self.file = folder / "tmdb-cache.json" if folder is not None else None
        self._entries: Dict[str, Tuple[float, Any]] = {}
        self._lock = threading.Lock()
        self._dirty = False
        self._load()

    def _load(self) -> None:
        if self.file is None or not self.file.is_file():
            return
        try:
            raw = json.loads(self.file.read_text(encoding="utf-8"))
            for name, entry in (raw.get("entries") or {}).items():
                if (isinstance(name, str) and isinstance(entry, list) and len(entry) == 2
                        and isinstance(entry[0], (int, float)) and not isinstance(entry[0], bool)):
                    self._entries[name] = (float(entry[0]), entry[1])
        except (OSError, ValueError, AttributeError) as exc:
            log.warning("TMDB cache unreadable (%s); starting fresh", exc)
            self._entries = {}

    def get(self, name: str, max_age: float, now: Optional[float] = None) -> Optional[Any]:
        now = time.time() if now is None else now
        with self._lock:
            entry = self._entries.get(name)
        if entry is None or not 0 <= now - entry[0] <= max_age:
            return None
        return entry[1]

    def put(self, name: str, data: Any, now: Optional[float] = None) -> None:
        with self._lock:
            self._entries[name] = (time.time() if now is None else now, data)
            self._dirty = True

    def __len__(self) -> int:
        return len(self._entries)

    def save(self) -> None:
        if self.file is None or not self._dirty:
            return
        with self._lock:
            newest = sorted(self._entries.items(), key=lambda item: -item[1][0])[:CACHE_ENTRIES]
            self._entries = dict(newest)
            data = json.dumps({"v": 1, "entries": {name: [at, value] for name, (at, value) in newest}},
                              ensure_ascii=False, separators=(",", ":"))
            self._dirty = False
        try:
            self.file.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.file.with_name(self.file.name + ".tmp")
            temporary.write_text(data, encoding="utf-8")
            os.replace(temporary, self.file)
        except OSError as exc:
            log.warning("couldn't save the TMDB cache: %s", exc)


# ---------------------------------------------------------------- asking TMDB
Opener = Callable[[urllib.request.Request, float], Any]


def _open(request: urllib.request.Request, timeout: float):
    return urllib.request.urlopen(request, timeout=timeout)


def _relay_said(exc: urllib.error.HTTPError) -> str:
    """The relay's {"error": "..."} word in a refusal ("" if it said nothing SyncWatch knows)."""
    try:
        said = json.loads(exc.read(2048).decode("utf-8")).get("error", "")
    except (OSError, ValueError, AttributeError, http.client.HTTPException):
        return ""
    return said if said in ("nokey", "key", "missing", "busy", "question", "parameter", "network", "tmdb") else ""


class Client:
    """Asks TMDB (one question at a time; meant for one background thread)."""

    def __init__(self, key: str, folder: Optional[Path], language: str = "en-US", region: str = "",
                 api: str = "", images: str = "", opener: Opener = _open, relay: str = ""):
        """relay: SyncWatch's relay (https://...), asked instead of TMDB when there's no key of one's own."""
        self.key = (key or "").strip()
        self.key_type = key_kind(self.key)
        self.relay = relay.strip().rstrip("/") if relay and not self.key else ""
        if self.relay:
            self.key_type = "relay"
        self.language = language if _LANGUAGE.match(language or "") else "en-US"
        self.region = region if _REGION.match(region or "") else ""
        self.api = self.relay + "/v1/tmdb" if self.relay else (api or API).rstrip("/")   # (tests point these
        self.images = images or IMAGES                                                   # elsewhere)
        self.posters = folder / "posters" if folder is not None else None
        self.cache = Cache(folder)
        self._open = opener
        self._last = 0.0
        self._written = 0

    # ------------------------------------------------------------ the questions
    def search(self, title: str, year: int = 0) -> List[Movie]:
        params = {"query": " ".join(title.split())[:200], "include_adult": "false"}
        if year:
            params["year"] = str(int(year))
        data = self._cached("search", "/search/movie", params,
                            lambda raw: {"results": clean_list(raw.get("results"), SEARCH_RESULTS)})
        return movies(data.get("results"))

    def find(self, title: str, year: int = 0) -> Optional[Movie]:
        """The film a file name means: searched with its year, then (a year off happens) without."""
        if not title:
            return None
        if not year:
            found = self.search(title)
            return found[0] if found else None
        for found in (self.search(title, year), self.search(title)):
            close = [movie for movie in found if movie.year and abs(movie.year - year) <= 1]
            if close:
                return close[0]
        return None

    def details(self, movie_id: int) -> Details:
        if not _movie_id(movie_id):
            raise TmdbError("missing", "no such film number")
        params = {"append_to_response": "videos,recommendations" + (",watch/providers" if self.region else "")}
        data = self._cached("movie", f"/movie/{int(movie_id)}", params,
                            lambda raw: clean_details(raw, self.region, self.language), extra=self.region)
        return to_details(data, self.region, self.language)

    def trending(self) -> List[Movie]:
        data = self._cached("trending", "/trending/movie/week", {},
                            lambda raw: {"results": clean_list(raw.get("results"))})
        return movies(data.get("results"))

    def poster(self, path: str, size: str = POSTER_SIZE) -> Optional[bytes]:
        """A poster's bytes (from the folder if it was fetched before), None if there's none."""
        if not isinstance(path, str) or not IMAGE_PATH.match(path) or size not in ("w92", "w154", "w185", "w342"):
            return None
        target = self.posters / f"{size}-{path[1:]}" if self.posters is not None else None
        if target is not None and target.is_file():
            try:
                os.utime(target)
                return target.read_bytes()
            except OSError:
                pass
        data = self._fetch_bytes(self.images + size + path, MAX_IMAGE, auth=False)
        if not data.startswith(_IMAGE_STARTS):
            raise TmdbError("odd", "not a picture")
        if target is not None:
            self._keep_poster(target, data)
        return data

    def save(self) -> None:
        self.cache.save()

    # ------------------------------------------------------------ the plumbing
    def _cached(self, kind: str, path: str, params: Dict[str, str], clean: Callable[[Any], Any],
                extra: str = "") -> Any:
        if not self.key_type:
            raise TmdbError("nokey" if not self.key else "key", "")
        name = f"{path}?{urllib.parse.urlencode(sorted(params.items()))}|{self.language}|{extra}"
        data = self.cache.get(name, AGES[kind])
        if data is None:
            raw = self._fetch_json(path, params)
            data = clean(raw)
            self.cache.put(name, data)
        return data

    def _fetch_json(self, path: str, params: Dict[str, str]) -> Dict[str, Any]:
        query = dict(params, language=self.language)
        if self.key_type == "key":
            query["api_key"] = self.key
        raw = self._fetch_bytes(f"{self.api}{path}?{urllib.parse.urlencode(query)}", MAX_ANSWER, auth=True)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise TmdbError("odd", "not JSON") from None
        if not isinstance(data, dict):
            raise TmdbError("odd", "not an object")
        return data

    def _fetch_bytes(self, url: str, limit: int, auth: bool) -> bytes:
        wait = self._last + GAP_S - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        headers = {"User-Agent": "SyncWatch", "Accept": "application/json" if auth else "image/*"}
        if auth and self.key_type == "token":
            headers["Authorization"] = f"Bearer {self.key}"
        try:
            with self._open(urllib.request.Request(url, headers=headers), TIMEOUT_S) as answer:
                data = answer.read(limit + 1)
        except urllib.error.HTTPError as exc:   # (its text has the code and reason, never the address)
            said = _relay_said(exc) if self.relay and auth else ""
            exc.close()
            if self.relay and auth:
                if said == "missing":
                    kind = "missing"
                elif exc.code == 429 or said in ("busy", "network", "tmdb") or (exc.code >= 500 and not said):
                    kind = "busy"
                else:       # no key there, TMDB refused it, an older relay, or not a relay at all
                    kind = "relay"
                raise TmdbError(kind, f"HTTP {exc.code} {said}".strip()) from None
            kind = {401: "key", 403: "key", 404: "missing", 429: "busy"}.get(exc.code)
            raise TmdbError(kind or ("busy" if exc.code >= 500 else "odd"), f"HTTP {exc.code}") from None
        except (urllib.error.URLError, http.client.HTTPException, OSError, ValueError) as exc:
            reason = getattr(exc, "reason", exc)
            raise TmdbError("network", type(reason).__name__ if not isinstance(reason, str) else reason) from None
        finally:
            self._last = time.monotonic()
        if len(data) > limit:
            raise TmdbError("odd", "answer too big")
        return data

    def _keep_poster(self, target: Path, data: bytes) -> None:
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_name(target.name + ".part")
            temporary.write_bytes(data)
            os.replace(temporary, target)
            self._written += 1
            if self._written % 50 == 1:
                files = sorted(target.parent.glob("w*-*"), key=lambda p: p.stat().st_mtime, reverse=True)
                for old in files[POSTER_FILES:]:
                    old.unlink(missing_ok=True)
        except OSError as exc:
            log.debug("poster not kept: %s", exc)
