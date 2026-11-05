"""A stand-in for TMDB (api.themoviedb.org and image.tmdb.org) on 127.0.0.1, for the tests.

    server = FakeTmdb().start()
    client = tmdb.Client(KEY, folder, region="NL", api=server.api, images=server.images)

A few made-up films (with TMDB's real numbers for well-known ones, so names line up) that
recommend each other, trailers, where-to-watch data for NL, and posters (tiny PNG pictures).
The key is checked like TMDB does (?api_key= or "Authorization: Bearer"); a wrong one gets 401.
``mode`` makes it misbehave: "busy" (429), "broken" (500), "odd" (not JSON), "huge" (too big).
``hits`` counts the questions per path, so tests can see the cache at work.
"""
from __future__ import annotations

import collections
import http.server
import json
import struct
import threading
import urllib.parse
import zlib

KEY = "0123456789abcdef0123456789abcdef"
TOKEN = "eyJhbGciOiJIUzI1NiJ9." + "eyJhdWQiOiJ0ZXN0In0" * 4 + ".c2lnbmF0dXJlLXNpZ25hdHVyZQ"


def _film(number, title, date, rating, votes, genres, poster, overview, recommends, trailer=""):
    return {"id": number, "title": title, "original_title": title, "release_date": date, "vote_average": rating,
            "vote_count": votes, "popularity": votes / 100.0, "genre_ids": genres, "poster_path": poster,
            "overview": overview, "adult": False, "_recommends": recommends, "_trailer": trailer}


FILMS = {f["id"]: f for f in [
    _film(603, "The Matrix", "1999-03-31", 8.2, 25000, [28, 878], "/matrix.jpg",
          "A hacker learns what the world really is.", [604, 27205, 157336], "vKQi3bBA1y8"),
    _film(604, "The Matrix Reloaded", "2003-05-15", 7.0, 11000, [28, 878], "/reloaded.jpg",
          "Neo and the rebels fight on.", [603, 27205]),
    _film(27205, "Inception", "2010-07-15", 8.4, 36000, [28, 878, 12], "/inception.jpg",
          "A thief who steals secrets from dreams.", [157336, 603, 155], "YoHD9XEInc0"),
    _film(157336, "Interstellar", "2014-11-05", 8.4, 34000, [12, 18, 878], "/interstellar.jpg",
          "Explorers travel through a wormhole.", [27205, 603, 155]),
    _film(155, "The Dark Knight", "2008-07-16", 8.5, 32000, [18, 28, 80], "/darkknight.jpg",
          "Batman meets the Joker.", [272, 27205]),
    _film(272, "Batman Begins", "2005-06-10", 7.7, 20000, [28, 80, 18], "/begins.jpg",
          "How Bruce Wayne became Batman.", [155]),
    _film(999001, "Obscure Gem", "2019-01-01", 10.0, 3, [18], "/gem.jpg", "Hardly anyone has seen it.", []),
]}


def png(width: int = 2, height: int = 3, rgb=(200, 40, 40)) -> bytes:
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    rows = b"".join(b"\x00" + bytes(rgb) * width for _ in range(height))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))


def public(film):
    return {k: v for k, v in film.items() if not k.startswith("_")}


class FakeTmdb:
    def __init__(self):
        self.mode = ""
        self.hits = collections.Counter()
        self.server = None

    def start(self) -> "FakeTmdb":
        fake = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):  # noqa: N802
                fake._answer(self)

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.api, self.images = base + "/3", base + "/t/p/"
        return self

    def stop(self) -> None:
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()

    def _answer(self, request) -> None:
        url = urllib.parse.urlsplit(request.path)
        query = dict(urllib.parse.parse_qsl(url.query))
        path = url.path
        self.hits[path] += 1
        if path.startswith("/t/p/"):
            name = path.rsplit("/", 1)[-1]
            if self.mode == "odd" or name.startswith("notapicture"):
                return self._send(request, 200, b"<html>hello</html>", "text/html")
            if any(film["poster_path"] == "/" + name for film in FILMS.values()):
                return self._send(request, 200, png(), "image/png")
            return self._send(request, 404, b"", "text/plain")
        bearer = request.headers.get("Authorization", "")
        if query.get("api_key") != KEY and bearer != f"Bearer {TOKEN}":
            return self._json(request, 401, {"status_code": 7, "status_message": "Invalid API key", "success": False})
        if self.mode == "busy":
            return self._json(request, 429, {"status_code": 25, "status_message": "Too many requests"})
        if self.mode == "broken":
            return self._json(request, 500, {"status_message": "Internal error"})
        if self.mode == "odd":
            return self._send(request, 200, b"this is not json", "application/json")
        if self.mode == "huge":
            return self._send(request, 200, b'{"results": "' + b"x" * 3_100_000 + b'"}', "application/json")
        if path == "/3/search/movie":
            words = set(query.get("query", "").lower().split())
            year = query.get("year")
            found = [public(f) for f in FILMS.values() if words and words <= set(f["title"].lower().split())
                     and (not year or f["release_date"].startswith(year))]
            found.sort(key=lambda f: -f["popularity"])
            return self._json(request, 200, {"page": 1, "results": found, "total_results": len(found)})
        if path == "/3/trending/movie/week":
            found = sorted((public(f) for f in FILMS.values()), key=lambda f: -f["popularity"])
            return self._json(request, 200, {"page": 1, "results": found})
        if path.startswith("/3/movie/"):
            try:
                film = FILMS[int(path.rsplit("/", 1)[-1])]
            except (KeyError, ValueError):
                return self._json(request, 404, {"status_code": 34, "status_message": "Not found"})
            answer = public(film)
            answer.pop("genre_ids")
            from syncwatch.discover.tmdb import GENRES
            answer.update({"runtime": 136, "tagline": "Welcome to the real world.",
                           "genres": [{"id": g, "name": GENRES[g]} for g in film["genre_ids"]]})
            extras = query.get("append_to_response", "").split(",")
            if "videos" in extras:
                videos = [{"site": "YouTube", "key": "teaserteas1", "type": "Teaser", "official": True,
                           "iso_639_1": "en"}]
                if film["_trailer"]:
                    videos.append({"site": "YouTube", "key": film["_trailer"], "type": "Trailer", "official": True,
                                   "iso_639_1": "en"})
                videos.append({"site": "Vimeo", "key": "12345", "type": "Trailer", "official": True})
                answer["videos"] = {"results": videos}
            if "recommendations" in extras:
                answer["recommendations"] = {"page": 1, "results": [public(FILMS[n]) for n in film["_recommends"]]}
            if "watch/providers" in extras:
                answer["watch/providers"] = {"results": {"NL": {
                    "link": f"https://www.themoviedb.org/movie/{film['id']}/watch?locale=NL",
                    "flatrate": [{"provider_name": "Netflix"}, {"provider_name": "HBO Max"}],
                    "rent": [{"provider_name": "Apple TV"}], "buy": [{"provider_name": "Apple TV"}]}}}
            return self._json(request, 200, answer)
        return self._json(request, 404, {"status_code": 34, "status_message": "Not found"})

    def _json(self, request, code, data) -> None:
        self._send(request, code, json.dumps(data).encode("utf-8"), "application/json")

    @staticmethod
    def _send(request, code, body, kind) -> None:
        request.send_response(code)
        request.send_header("Content-Type", kind)
        request.send_header("Content-Length", str(len(body)))
        request.end_headers()
        request.wfile.write(body)
