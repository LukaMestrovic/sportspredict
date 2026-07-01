"""Aggregate a referee discipline history table from API-Football fixtures.

API-Football has no fixtures-by-referee query, so we scan whole competitions
(config: ``referee.history_league_seasons``), group finished fixtures by the
``fixture.referee`` field, and pull per-fixture discipline counts: fouls/cards from
``fixtures/statistics`` and penalties awarded from ``fixtures/events`` (~2 cached calls
per fixture). The output table feeds :func:`sportspredict.ingest.referee.build_referee_multipliers`.

Build/refresh via ``sportspredict referee-history``.
"""

from __future__ import annotations

import pandas as pd

from . import apifootball as af
from .referee import referee_key

# Fixture status codes that count as a completed match.
_FINISHED = {"FT", "AET", "PEN"}

HISTORY_COLUMNS = ["referee", "matches", "yellows_per_match", "fouls_per_match", "pens_per_match"]


def build_history(league_seasons: list[tuple[int, int]], log=print) -> pd.DataFrame:
    """Return a history frame with :data:`HISTORY_COLUMNS` for the given (league, season) pairs."""
    rows: list[dict] = []
    for league, season in league_seasons:
        fixtures = af.fixtures(league=int(league), season=int(season))
        used = 0
        for fx in fixtures:
            info = fx.get("fixture") or {}
            fid = info.get("id")
            ref = info.get("referee")
            status = ((info.get("status") or {}).get("short")) or ""
            if not fid or not ref or status not in _FINISHED:
                continue
            try:
                disc = af.parse_discipline(af.fixture_statistics(fid, half=False))
                pens = af.parse_penalties(af.fixture_events(fid))
            except Exception as e:  # quota / transient API failure: skip, keep building
                log(f"  fixture {fid}: discipline unavailable ({e})")
                continue
            if not disc:
                continue
            rows.append(
                {"referee": referee_key(ref), "yellows": disc["yellows"],
                 "fouls": disc["fouls"], "pens": float(pens)}
            )
            used += 1
        log(f"league={league} season={season}: {used} fixtures with referee + discipline")

    if not rows:
        return pd.DataFrame(columns=HISTORY_COLUMNS)
    df = pd.DataFrame(rows)
    return df.groupby("referee", as_index=False).agg(
        matches=("referee", "size"),
        yellows_per_match=("yellows", "mean"),
        fouls_per_match=("fouls", "mean"),
        pens_per_match=("pens", "mean"),
    )[HISTORY_COLUMNS]
