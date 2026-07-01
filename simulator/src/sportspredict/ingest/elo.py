"""Elo strength ratings.

Either load a precomputed Elo table (eloratings.net / Kaggle mirror) or compute World-Football-
style Elo from a results frame, so the strength feature is available even fully offline. The
computed variant returns the latest rating per team and a lookup as-of any date for
time-correct backtesting.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd


def _expected(r_a: float, r_b: float) -> float:
    return 1.0 / (1.0 + 10.0 ** ((r_b - r_a) / 400.0))


@dataclass
class EloModel:
    k: float = 30.0
    home_adv: float = 65.0
    base: float = 1500.0
    ratings: dict[str, float] = field(default_factory=dict)
    history: list[tuple] = field(default_factory=list)  # (date, team, rating)

    def fit(self, results: pd.DataFrame) -> "EloModel":
        """Sequentially update ratings over a date-sorted results frame."""
        df = results.sort_values("date")
        for row in df.itertuples(index=False):
            ha = self.ratings.get(row.home_team, self.base)
            aa = self.ratings.get(row.away_team, self.base)
            adv = 0.0 if getattr(row, "neutral", False) else self.home_adv
            exp_h = _expected(ha + adv, aa)
            if row.home_score > row.away_score:
                s_h = 1.0
            elif row.home_score < row.away_score:
                s_h = 0.0
            else:
                s_h = 0.5
            # Goal-difference weighting (World Football Elo style).
            gd = abs(row.home_score - row.away_score)
            mult = 1.0 if gd <= 1 else (1.5 if gd == 2 else (11 + gd) / 8.0)
            delta = self.k * mult * (s_h - exp_h)
            self.ratings[row.home_team] = ha + delta
            self.ratings[row.away_team] = aa - delta
            self.history.append((row.date, row.home_team, self.ratings[row.home_team]))
            self.history.append((row.date, row.away_team, self.ratings[row.away_team]))
        return self

    def rating(self, team: str) -> float:
        return self.ratings.get(team, self.base)

    def diff(self, team_a: str, team_b: str) -> float:
        return self.rating(team_a) - self.rating(team_b)


def load_elo_table(path: str) -> dict[str, float]:
    """Load a precomputed Elo CSV with columns ``team, rating`` -> latest rating per team."""
    df = pd.read_csv(path)
    df = df.sort_values("rating").drop_duplicates("team", keep="last")
    return dict(zip(df["team"], df["rating"].astype(float)))
