"""Referee histories -> per-referee discipline multipliers.

The pure function :func:`build_referee_multipliers` turns a referee history table (cards /
fouls / penalties per match) into multipliers relative to the global mean, **shrunk** toward
1.0 for referees with few matches (empirical-Bayes style) so a small sample does not produce
extreme adjustments. Source the history from API-Football fixtures or a scraped
WorldReferee/Transfermarkt table; this transform is source-agnostic and unit-tested.
"""

from __future__ import annotations

import re
import unicodedata

import pandas as pd


def referee_key(name: str) -> str:
    """Normalized referee identity: accent-folded ``"<lastname> <first initial>"``.

    API-Football writes the same official differently across competitions
    ("Szymon Marciniak", "S. Marciniak", "Szymon Marciniak, Poland"), so both the
    history table and the lookup must go through this key.
    """
    s = unicodedata.normalize("NFKD", str(name or "")).encode("ascii", "ignore").decode()
    s = s.split(",")[0]  # drop a trailing ", Country"
    parts = re.sub(r"[^a-z. ]", " ", s.lower()).split()
    if not parts:
        return ""
    last = parts[-1].strip(".")
    initial = parts[0][0] if len(parts) > 1 else ""
    return f"{last} {initial}".strip()


def build_referee_multipliers(history: pd.DataFrame, shrink_matches: float = 10.0) -> dict[str, dict]:
    """Return ``{referee: {card_mult, foul_mult, pen_mult}}``.

    ``history`` columns: ``referee, matches, yellows_per_match, fouls_per_match,
    pens_per_match``. The shrinkage prior is the match-weighted global mean; ``shrink_matches``
    is the prior strength in match-equivalents.
    """
    cols = {
        "card_mult": "yellows_per_match",
        "foul_mult": "fouls_per_match",
        "pen_mult": "pens_per_match",
    }
    weights = history["matches"].astype(float)
    globals_ = {
        out: float((history[col] * weights).sum() / weights.sum())
        for out, col in cols.items()
    }

    result: dict[str, dict] = {}
    for row in history.itertuples(index=False):
        n = float(row.matches)
        mult = {}
        for out, col in cols.items():
            g = globals_[out]
            raw = float(getattr(row, col))
            # Shrink the referee's rate toward the global mean, then express as a multiplier.
            shrunk = (n * raw + shrink_matches * g) / (n + shrink_matches)
            mult[out] = shrunk / g if g > 0 else 1.0
        result[referee_key(row.referee) or str(row.referee)] = mult
    return result
