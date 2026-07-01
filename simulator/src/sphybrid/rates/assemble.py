from __future__ import annotations

from pathlib import Path

import pandas as pd

def results_from_stat_table(table: pd.DataFrame) -> pd.DataFrame:
    """Derive a home/away score table (for fitting team attack/defense ratings) from match stats."""
    hg = table["home_goals_h1"].to_numpy() + table["home_goals_h2"].to_numpy()
    ag = table["away_goals_h1"].to_numpy() + table["away_goals_h2"].to_numpy()
    neutral = table["neutral"].astype(bool).to_numpy() if "neutral" in table else True
    return pd.DataFrame({
        "home_team": table["home_team"].astype(str).to_numpy(),
        "away_team": table["away_team"].astype(str).to_numpy(),
        "home_score": hg,
        "away_score": ag,
        "neutral": neutral,
    })

def load_stat_table(path: str | Path) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Load the historical match stat table and derive the results table for team ratings.

    The table is expected to already carry per-half stat columns plus ``home_elo``/``away_elo``,
    ``stage`` and ``tournament`` (this repo ships ``data/processed/history_stat_table.parquet``).
    """
    table = pd.read_parquet(path).copy()
    if "home_elo" not in table.columns:
        table["home_elo"] = 1500.0
    if "away_elo" not in table.columns:
        table["away_elo"] = 1500.0
    if "stage" not in table.columns:
        table["stage"] = "group"
    if "tournament" not in table.columns:
        table["tournament"] = "all"
    return table, results_from_stat_table(table)
