"""What should we watch? (0.7): film names, TMDB (against a stand-in on this PC), picks, the vote."""
import json
import os
import random
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
os.environ["no_proxy"] = os.environ["NO_PROXY"] = "127.0.0.1,localhost"

from fake_tmdb import KEY, TOKEN, FakeTmdb  # noqa: E402

from syncwatch.discover import names, picks, tmdb  # noqa: E402
from syncwatch.discover.tmdb import Movie, TmdbError  # noqa: E402
from syncwatch.discover.vote import MAX_CHOICES, PER_PERSON, Vote, VoteFull  # noqa: E402
from syncwatch.net import protocol  # noqa: E402


class NameTests(unittest.TestCase):
    CASES = [
        ("The.Matrix.1999.1080p.BluRay.x264-GROUP.mkv", "The Matrix", 1999),
        ("Inception (2010) {tmdb-27205}.mkv", "Inception", 2010),
        ("2001.A.Space.Odyssey.1968.REMASTERED.1080p.BluRay.x264.mkv", "2001 A Space Odyssey", 1968),
        ("1917.2019.1080p.mkv", "1917", 2019),
        ("Wonder.Woman.1984.2020.1080p.WEBRip.mkv", "Wonder Woman 1984", 2020),
        ("Blade.Runner.2049.2017.2160p.mkv", "Blade Runner 2049", 2017),
        ("Spider-Man.No.Way.Home.2021.1080p.WEB-DL.DDP5.1.H.264-EVO.mkv", "Spider-Man No Way Home", 2021),
        ("[YTS.MX] Dune Part Two (2024) [1080p].mp4", "Dune Part Two", 2024),
        ("www.SomeSite.com - Movie Name (2022) 1080p.mkv", "Movie Name", 2022),
        ("the_shawshank_redemption_1994.avi", "The Shawshank Redemption", 1994),
        ("Movie.mkv", "Movie", 0),
        ("Amélie.2001.mkv", "Amélie", 2001),
        ("Lord.of.the.Rings.The.Fellowship.of.the.Ring.Extended.Edition.2001.1080p.mkv",
         "Lord of the Rings The Fellowship of the Ring", 2001),
        ("Blade.Runner.1982.The.Final.Cut.1080p.mkv", "Blade Runner", 1982),
        ("The.Final.Cut.2004.mkv", "The Final Cut", 2004),
        ("1984.mkv", "1984", 0),
        ("Oppenheimer 2023 1080p WEB-DL x264 AAC - Group.mp4", "Oppenheimer", 2023),
        ("Aliens.Directors.Cut.1986.mkv", "Aliens", 1986),
        ("Up.2009.mkv", "Up", 2009),
        ("Se7en.1995.mkv", "Se7en", 1995),
        ("Avatar.The.Way.of.Water.2022.HDR10+.2160p.mkv", "Avatar The Way of Water", 2022),
        ("Mad.Max.Fury.Road.2015.1080p.BluRay.DTS-HD.MA.7.1.x264.mkv", "Mad Max Fury Road", 2015),
        (r"C:\Films\Interstellar.2014.IMAX.1080p.mkv", "Interstellar", 2014),
    ]

    def test_release_names(self):
        for file_name, title, year in self.CASES:
            with self.subTest(file_name):
                found = names.guess(file_name)
                self.assertIsNotNone(found)
                self.assertEqual((found.title, found.year), (title, year))

    def test_ids_written_into_names(self):
        self.assertEqual(names.guess("Inception (2010) {tmdb-27205}.mkv").tmdb, 27205)
        self.assertEqual(names.guess("Parasite (2019) [tmdbid-496243].mkv").tmdb, 496243)
        self.assertEqual(names.guess("Film (2020) {imdb-tt1234567}.mkv"), names.Guess("Film", 2020))

    def test_episodes_and_nothing_are_not_films(self):
        for file_name in ("Breaking.Bad.S01E01.Pilot.720p.mkv", "Some Show 1x02.mkv", "Show Season 2 Episode 3.mkv",
                          ".mkv", "[]..mkv", "-.avi"):
            with self.subTest(file_name):
                self.assertIsNone(names.guess(file_name))

    def test_same_film_under_another_name(self):
        a = names.guess("The.Matrix.1999.1080p.mkv")
        b = names.guess("the matrix (1999).mp4")
        self.assertEqual(a.key, b.key)
        self.assertEqual(names.simplify("Amélie: The Movie!"), "amelie the movie")
        self.assertEqual(names.simplify("Fast & Furious"), "fast and furious")
        self.assertEqual(a.label, "The Matrix (1999)")


class CopyTests(unittest.TestCase):
    """"Auto-detect what to download": the host's film may be in my library under another name."""
    MINE = [("Matrix (1999) 720p.mkv", 1_400_000_000, "a"), ("Inception.2010.mkv", 2_000_000_000, "b"),
            ("renamed.mkv", 4_321_000_000, "c"), ("Dune.2021.mkv", 3_000_000_000, "d")]

    def test_the_same_file_or_the_same_film(self):
        self.assertEqual(names.find_copy(self.MINE, "Inception.2010.mkv", 2_000_000_000), "b")
        self.assertEqual(names.find_copy(self.MINE, "Some.Film.2015.1080p.mkv", 4_321_000_000), "c")  # renamed
        self.assertEqual(names.find_copy(self.MINE, "The.Matrix.1999.1080p.BluRay.x264.mkv", 9), "a")  # other release
        self.assertIsNone(names.find_copy(self.MINE, "Dune.Part.Two.2024.mkv", 5))
        self.assertIsNone(names.find_copy(self.MINE, "Dune.1984.mkv", 5))      # the other Dune
        self.assertEqual(names.find_copy(self.MINE, "Dune.mkv", 5), "d")      # no year: the title counts
        self.assertIsNone(names.find_copy([], "Dune.mkv", 5))
        self.assertTrue(names.same_film("Film (2020) {tmdb-5}.mkv", "Other Name [tmdbid-5].mkv"))


class TmdbTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = FakeTmdb().start()

    @classmethod
    def tearDownClass(cls):
        cls.server.stop()

    def setUp(self):
        self.folder = Path(tempfile.mkdtemp(prefix="sw-tmdb-"))
        self.server.mode = ""
        self.server.hits.clear()

    def tearDown(self):
        shutil.rmtree(self.folder, ignore_errors=True)

    def client(self, key=KEY, region="NL", folder=None):
        return tmdb.Client(key, folder or self.folder, region=region, api=self.server.api, images=self.server.images)

    def test_key_kinds(self):
        self.assertEqual(tmdb.key_kind(KEY), "key")
        self.assertEqual(tmdb.key_kind(" " + KEY + " "), "key")
        self.assertEqual(tmdb.key_kind(TOKEN), "token")
        for junk in ("", "abc", KEY.upper(), KEY + "0", "a.b.c", "x" * 2000, "eyJ a.b.c"):
            self.assertEqual(tmdb.key_kind(junk), "", junk)

    def test_search_with_either_kind_of_key(self):
        for key in (KEY, TOKEN):
            with self.subTest(key=key[:8]):
                found = self.client(key, folder=self.folder / key[:4]).search("matrix", 1999)
                self.assertEqual([m.id for m in found], [603])
                matrix = found[0]
                self.assertEqual((matrix.title, matrix.year, matrix.poster), ("The Matrix", 1999, "/matrix.jpg"))
                self.assertEqual(matrix.genres, ("Action", "Science Fiction"))

    def test_bad_or_missing_key(self):
        with self.assertRaises(TmdbError) as caught:
            self.client("f" * 32).search("matrix")
        self.assertEqual(caught.exception.kind, "key")
        with self.assertRaises(TmdbError) as caught:
            self.client("").search("matrix")
        self.assertEqual(caught.exception.kind, "nokey")
        with self.assertRaises(TmdbError) as caught:
            self.client("not a key").search("matrix")
        self.assertEqual(caught.exception.kind, "key")
        self.assertEqual(sum(self.server.hits.values()), 1)     # the malformed ones aren't even sent

    def test_finding_a_files_film(self):
        client = self.client()
        self.assertEqual(client.find("The Matrix", 1999).id, 603)
        self.assertEqual(client.find("Inception", 2011).id, 27205)      # a year off happens
        self.assertIsNone(client.find("Inception", 1990))               # far off: not this film
        self.assertEqual(client.find("Inception").id, 27205)
        self.assertIsNone(client.find("No Such Film Anywhere", 2001))
        self.assertIsNone(client.find(""))

    def test_details_trailer_where_to_watch_and_recommendations(self):
        details = self.client().details(603)
        self.assertEqual(details.movie.label, "The Matrix (1999)")
        self.assertEqual(details.runtime, 136)
        self.assertEqual(details.trailer, "vKQi3bBA1y8")                # the official trailer, not the teaser
        self.assertEqual([m.id for m in details.recommendations], [604, 27205, 157336])
        self.assertEqual(details.providers.stream, ("Netflix", "HBO Max"))
        self.assertEqual(details.providers.rent, ("Apple TV",))
        self.assertTrue(details.providers.link.startswith("https://www.themoviedb.org/movie/603/watch"))
        self.assertEqual(details.providers.summary(), "Streaming on Netflix, HBO Max; to rent on Apple TV")
        no_region = self.client(region="").details(604)
        self.assertIsNone(no_region.providers)
        self.assertEqual(self.client().details(604).trailer, "teaserteas1")   # no trailer: the teaser
        with self.assertRaises(TmdbError) as caught:
            self.client().details(424242)
        self.assertEqual(caught.exception.kind, "missing")

    def test_the_cache_saves_questions_and_never_holds_the_key(self):
        client = self.client()
        client.details(603)
        client.search("inception", 2010)
        client.trending()
        asked = sum(self.server.hits.values())
        client.details(603)
        client.search("inception", 2010)
        client.trending()
        self.assertEqual(sum(self.server.hits.values()), asked)
        client.save()
        text = (self.folder / "tmdb-cache.json").read_text(encoding="utf-8")
        self.assertNotIn(KEY, text)
        again = self.client()                                   # another start: from the file
        self.assertEqual(again.details(603).trailer, "vKQi3bBA1y8")
        self.assertEqual(sum(self.server.hits.values()), asked)
        name = next(n for n in again.cache._entries if n.startswith("/trending"))
        at, data = again.cache._entries[name]
        again.cache._entries[name] = (at - tmdb.AGES["trending"] - 60, data)   # too old: asked again
        again.trending()
        self.assertEqual(sum(self.server.hits.values()), asked + 1)

    def test_a_tampered_cache_is_checked_again(self):
        client = self.client()
        client.details(603)
        client.save()
        path = self.folder / "tmdb-cache.json"
        raw = json.loads(path.read_text(encoding="utf-8"))
        for name, (_at, data) in raw["entries"].items():
            data["poster_path"] = "/../../evil.exe"
            data["trailer"] = "not a video id at all"
            data["providers"]["link"] = "https://evil.example/"
            data["recommendations"]["results"].append({"id": "1", "title": 5})
        path.write_text(json.dumps(raw), encoding="utf-8")
        details = self.client().details(603)
        self.assertEqual((details.movie.poster, details.trailer, details.providers), ("", "", None))
        self.assertEqual(len(details.recommendations), 3)
        path.write_text("{broken", encoding="utf-8")
        self.assertEqual(self.client().details(603).movie.id, 603)     # a broken cache: asked afresh

    def test_odd_answers_are_cleaned(self):
        raw = {"id": 5, "title": "  Real\x00 \n Title ", "release_date": "20x1-01-01", "poster_path": "/a/../b.jpg",
               "vote_average": 42, "vote_count": -5, "popularity": float("nan"), "genre_ids": [28, "x", True, 99999],
               "overview": "y" * 5000}
        movie = tmdb.to_movie(raw)
        self.assertEqual((movie.title, movie.year, movie.poster, movie.rating, movie.votes, movie.popularity),
                         ("Real Title", 0, "", 10.0, 0, 0.0))
        self.assertEqual(movie.genres, ("Action",))
        self.assertEqual(len(movie.overview), 1500)
        for junk in ({"id": 0, "title": "x"}, {"id": True, "title": "x"}, {"id": 1}, {"id": 1, "title": ""},
                     {"id": 1, "title": "x", "adult": True}, "film", None, [1]):
            self.assertIsNone(tmdb.to_movie(junk), junk)
        self.assertEqual(len(tmdb.clean_list([{"id": 1, "title": "a"}] * 3 + [{"id": 2, "title": "b"}])), 2)

    def test_tmdb_having_trouble(self):
        client = self.client()
        for mode, kind in (("busy", "busy"), ("broken", "busy"), ("odd", "odd"), ("huge", "odd")):
            self.server.mode = mode
            with self.subTest(mode), self.assertRaises(TmdbError) as caught:
                client.search(f"matrix {mode}")
            self.assertEqual(caught.exception.kind, kind)
            self.assertNotIn(KEY, str(caught.exception))
        unreachable = tmdb.Client(KEY, self.folder, api="http://127.0.0.1:9/3", images="http://127.0.0.1:9/t/p/")
        with self.assertRaises(TmdbError) as caught:
            unreachable.search("matrix")
        self.assertEqual(caught.exception.kind, "network")
        self.assertNotIn(KEY, str(caught.exception))

    def test_posters(self):
        client = self.client()
        data = client.poster("/matrix.jpg")
        self.assertTrue(data.startswith(b"\x89PNG"))
        self.assertEqual(client.poster("/matrix.jpg"), data)            # the second time: from the folder
        self.assertEqual(self.server.hits["/t/p/w185/matrix.jpg"], 1)
        for bad in ("/../x.jpg", "matrix.jpg", "/x.exe", "", None, "/a/b.jpg"):
            self.assertIsNone(client.poster(bad))
        self.assertIsNone(client.poster("/matrix.jpg", "original"))
        with self.assertRaises(TmdbError):
            client.poster("/notapicture.jpg")                           # not a picture: refused
        self.assertFalse((self.folder / "posters" / "w185-notapicture.jpg").exists())


class PickTests(unittest.TestCase):
    def movie(self, number, rating=7.5, votes=5000, title=None):
        return Movie(number, title or f"Film {number}", 2000, rating=rating, votes=votes)

    def test_films_several_of_yours_recommend_come_first(self):
        matrix, inception = self.movie(1, title="The Matrix"), self.movie(2, title="Inception")
        recs = {1: [self.movie(10), self.movie(11), self.movie(2)], 2: [self.movie(11), self.movie(12), self.movie(1)]}
        found = picks.combine([picks.Seed(matrix, 1.0), picks.Seed(inception, 1.0)], recs)
        self.assertEqual(found[0].movie.id, 11)
        self.assertEqual(sorted(found[0].because), ["Inception", "The Matrix"])
        self.assertNotIn(1, [p.movie.id for p in found])        # yours are never suggested
        self.assertNotIn(2, [p.movie.id for p in found])
        self.assertEqual(found[1].because, ["The Matrix"])
        skipped = picks.combine([picks.Seed(matrix, 1.0)], recs, skip=[10])
        self.assertNotIn(10, [p.movie.id for p in skipped])
        self.assertEqual(len(picks.combine([picks.Seed(matrix, 1.0)], recs, limit=1)), 1)

    def test_weights_and_quality(self):
        now = time.time()
        self.assertGreater(picks.seed_weight(now, now), picks.seed_weight(now - 60 * 86400, now))
        self.assertGreater(picks.seed_weight(now - 400 * 86400, now), picks.seed_weight(0, now))
        gem, classic = self.movie(1, 10.0, 3), self.movie(2, 8.5, 30000)
        self.assertGreater(picks.quality(classic), picks.quality(gem))  # 3 votes of 10/10 isn't a 10
        self.assertAlmostEqual(picks.quality(self.movie(3, 0, 0)), 0.64)

    def test_pick_one_for_me(self):
        found = [picks.Pick(self.movie(n), 10.0 - n) for n in range(15)]
        rng = random.Random(7)
        chosen = {picks.surprise(found, rng=rng).movie.id for _ in range(200)}
        self.assertTrue(chosen <= set(range(10)))
        self.assertGreater(len(chosen), 3)
        self.assertIsNone(picks.surprise([]))


class VoteModelTests(unittest.TestCase):
    def setUp(self):
        self.vote = Vote()
        self.vote.start()

    def test_suggest_vote_change_and_win(self):
        matrix = self.vote.suggest("The Matrix", 1999, 603, "/matrix.jpg", "Alex", by_id="alex")
        inception = self.vote.suggest("Inception", 2010, 27205, "", "Marek", by_id="marek")
        self.assertIs(self.vote.suggest("the matrix", 1999, 603, "", "Kuba", by_id="kuba"), matrix)
        self.assertIs(self.vote.suggest("The  Matrix", 0, 0, "", "Kuba", by_id="kuba"), matrix)   # no TMDB number
        self.vote.cast("alex", "Alex", matrix.key)
        self.vote.cast("marek", "Marek", matrix.key)
        self.vote.cast("kuba", "Kuba", inception.key)
        self.assertEqual(matrix.voters, ["Alex", "Marek"])
        self.vote.cast("marek", "Marek", inception.key)             # changed his mind
        self.assertEqual((matrix.voters, inception.voters), (["Alex"], ["Kuba", "Marek"]))
        self.vote.cast("kuba", "Kuba", 0)                           # took it back
        self.assertEqual(inception.voters, ["Marek"])
        self.assertFalse(self.vote.cast("kuba", "Kuba", 999))
        self.vote.mark_have("kuba", "Kuba", [matrix.key, 999])
        self.assertEqual(matrix.have, ["Kuba"])
        self.assertIs(self.vote.close(), matrix)                    # 1:1, more people have The Matrix
        self.assertFalse(self.vote.open)
        self.assertTrue(self.vote.active)
        self.assertFalse(self.vote.cast("alex", "Alex", inception.key))   # closed: no more votes
        self.assertIs(self.vote.close(), matrix)

    def test_limits(self):
        for n in range(PER_PERSON):
            self.vote.suggest(f"Film {n}", 2000, 0, "", "Alex", by_id="alex")
        with self.assertRaises(VoteFull):
            self.vote.suggest("One too many", 2000, 0, "", "Alex", by_id="alex")
        for n in range(MAX_CHOICES - PER_PERSON):
            self.vote.suggest(f"Other {n}", 2000, 0, "", f"P{n}", by_id=f"p{n}")
        with self.assertRaises(VoteFull):
            self.vote.suggest("Thirteenth", 2000, 0, "", "Late", by_id="late")
        with self.assertRaises(ValueError):
            self.vote.suggest("   ", 2000, 0, "", "Late", by_id="late")

    def test_taking_back_leaving_and_ties(self):
        a = self.vote.suggest("A", 0, 0, "", "Alex", by_id="alex")
        b = self.vote.suggest("B", 0, 0, "", "Marek", by_id="marek")
        self.vote.cast("marek", "Marek", b.key)
        self.vote.cast("kuba", "Kuba", b.key)
        self.vote.cast("alex", "Alex", a.key)
        self.assertTrue(self.vote.keep_only(["alex", "marek"]))     # Kuba left
        self.assertEqual(b.voters, ["Marek"])
        self.assertFalse(self.vote.keep_only(["alex", "marek"]))
        self.vote.withdraw(b.key)
        self.assertEqual(self.vote.ballot("marek"), 0)
        self.assertEqual([c.key for c in self.vote.choices], [a.key])
        c = self.vote.suggest("C", 0, 0, "", "Marek", by_id="marek")
        self.vote.cast("marek", "Marek", c.key)
        self.assertIs(self.vote.close(), a)                         # 1:1, nobody has either: the first one

    def test_nothing_suggested_and_cancel(self):
        self.assertIsNone(self.vote.close())
        self.assertFalse(self.vote.active)
        self.vote.start()
        self.vote.suggest("A", 0, 0, "", "Alex")
        self.vote.cancel()
        self.assertEqual((self.vote.open, self.vote.choices, self.vote.active), (False, [], False))

    def test_the_room_message(self):
        a = self.vote.suggest("The Matrix", 1999, 603, "/matrix.jpg", "Alex", by_id="alex")
        self.vote.cast("alex", "Alex", a.key)
        self.vote.mark_have("marek", "Marek", [a.key])
        body = {"t": "vote", "s": "0" * 16, "n": 1, **self.vote.to_message()}
        checked = protocol.validate(protocol.ROOM, json.loads(json.dumps(body)))
        copy = Vote()
        self.assertTrue(copy.load_message(checked))
        self.assertEqual(copy.choices[0].to_message(), a.to_message())
        self.assertTrue(copy.open)
        stale = dict(checked, no=checked["no"] - 1)
        self.assertFalse(copy.load_message(stale))
        copy.reset()
        self.assertEqual((copy.choices, copy.version), ([], 0))


class VoteProtocolTests(unittest.TestCase):
    def body(self, **fields):
        return {"t": "vote", "s": "0" * 16, "n": 1, "no": 1, "open": True, **fields}

    def choice(self, **fields):
        return {"k": 1, "title": "Film", "year": 2000, "tmdb": 1, "by": "Alex", "voters": [], "have": [], **fields}

    def test_limits_hold(self):
        full = [self.choice(k=n + 1, voters=["x" * 32] * 64, have=["y" * 32] * 64) for n in range(12)]
        protocol.validate(protocol.ROOM, self.body(choices=full))
        for broken in ([self.choice(k=n + 1) for n in range(13)], [self.choice(poster="/../x.jpg")],
                       [self.choice(poster="http://evil/x.jpg")], [self.choice(voters=["x" * 33])],
                       [self.choice(voters=[5])], [self.choice(have=["a"] * 65)], [self.choice(year=99999)],
                       [{"k": 1}]):
            with self.subTest(broken=str(broken)[:60]), self.assertRaises(protocol.BadMessage):
                protocol.validate(protocol.ROOM, self.body(choices=broken))
        request = {"t": "vreq", "s": "0" * 16, "n": 1}
        protocol.validate(protocol.ROOM, dict(request, op="have", keys=list(range(1, 13))))
        for broken in (dict(op="have", keys=list(range(1, 14))), dict(op="close"), dict(op="cast", k=-1),
                       dict(op="have", keys=[0]), dict(op="suggest", poster="x.png")):
            with self.subTest(broken=broken), self.assertRaises(protocol.BadMessage):
                protocol.validate(protocol.ROOM, dict(request, **broken))


if __name__ == "__main__":
    unittest.main()
