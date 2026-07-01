"""International results ingestion (Kaggle "International football results 1872-2026").

Large sample of national-team results used for the goals/strength model and 1X2 calibration.
Pure loaders/filters; no network. Expected CSV columns: ``date, home_team, away_team,
home_score, away_score, tournament, neutral``.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

_WC_TOURNAMENTS = {"FIFA World Cup", "FIFA World Cup qualification"}


def load_results(path: str | Path) -> pd.DataFrame:
    df = pd.read_csv(path, parse_dates=["date"])
    df = df.dropna(subset=["home_score", "away_score"]).copy()
    df["home_score"] = df["home_score"].astype(int)
    df["away_score"] = df["away_score"].astype(int)
    if "neutral" in df.columns:
        df["neutral"] = df["neutral"].astype(bool)
    return df


def competitive_only(df: pd.DataFrame, since: str | None = "2002-01-01") -> pd.DataFrame:
    """Keep competitive internationals (drop friendlies); optionally restrict by date."""
    out = df
    if "tournament" in out.columns:
        out = out[out["tournament"].str.lower() != "friendly"]
    if since is not None:
        out = out[out["date"] >= pd.Timestamp(since)]
    return out.reset_index(drop=True)
