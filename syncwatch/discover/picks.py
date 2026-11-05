""""For you": films to watch next, worked out from the films you have and the ones you've watched.

Every film of yours that TMDB knows brings its own recommendations (TMDB's, from what people who
liked it also liked). A film recommended by several of yours, or high up in their lists, scores
more; films you watched lately count more than ones you only have; well-rated films (trusted more
the more people rated them) come first. Films you have or watched are never suggested.

    picks = combine([Seed(matrix, 2.0), Seed(inception, 0.8)], {603: [...], 27205: [...]})
    picks[0].movie, picks[0].because      # Movie(...), ["The Matrix", "Inception"]
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Set

from .tmdb import Movie

DAY = 86400.0


@dataclass(frozen=True)
class Seed:
    movie: Movie
    weight: float           # how much this film counts


@dataclass
class Pick:
    movie: Movie
    score: float
    because: List[str] = field(default_factory=list)    # your films that led here, strongest first (≤ 2)


def seed_weight(watched_at: float, now: float) -> float:
    """Watched today 2.0, two months ago ~1.4, long ago 1.0; only in the library (never opened) 0.8."""
    if watched_at <= 0:
        return 0.8
    days = max(0.0, (now - watched_at) / DAY)
    return 1.0 + math.exp(-days / 60.0)


def quality(movie: Movie) -> float:
    """0-1 from TMDB's rating, pulled towards an average film when only a few people rated it."""
    votes, trust, average = max(0, movie.votes), 300.0, 6.4
    return (votes / (votes + trust) * movie.rating + trust / (votes + trust) * average) / 10.0


def combine(seeds: Sequence[Seed], recommendations: Dict[int, Sequence[Movie]],
            skip: Iterable[int] = (), limit: int = 40) -> List[Pick]:
    mine: Set[int] = {seed.movie.id for seed in seeds} | set(skip)
    scores: Dict[int, float] = {}
    found: Dict[int, Movie] = {}
    reasons: Dict[int, Dict[str, float]] = {}
    for seed in seeds:
        listed = recommendations.get(seed.movie.id) or ()
        for rank, movie in enumerate(listed):
            if movie.id in mine:
                continue
            share = seed.weight * (1.0 - rank / (len(listed) + 5.0))
            scores[movie.id] = scores.get(movie.id, 0.0) + share
            found.setdefault(movie.id, movie)
            why = reasons.setdefault(movie.id, {})
            why[seed.movie.title] = why.get(seed.movie.title, 0.0) + share
    picks = [Pick(found[number], round(score * (0.5 + quality(found[number])), 4),
                  [title for title, _ in sorted(reasons[number].items(), key=lambda kv: (-kv[1], kv[0]))][:2])
             for number, score in scores.items()]
    picks.sort(key=lambda pick: (-pick.score, -pick.movie.popularity, pick.movie.id))
    return picks[:limit]


def surprise(picks: Sequence[Pick], top: int = 10, rng: Optional[random.Random] = None) -> Optional[Pick]:
    """"Pick one for me": one of the best few, the better ones more likely."""
    best = list(picks[:top])
    if not best:
        return None
    rng = rng or random.Random()
    return rng.choices(best, weights=[max(0.01, pick.score) for pick in best], k=1)[0]
