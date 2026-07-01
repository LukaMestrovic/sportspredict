"""Club player rates via ``soccerdata`` (FBref) -> score/assist priors.

International samples are tiny, so player goal/assist shares lean on club form. This wraps
``soccerdata.FBref`` (lazy import) to produce per-player non-penalty goals/90 and assists/90,
which map directly onto :class:`~sportspredict.features.context.PlayerInfo` weights.
"""

from __future__ import annotations

import pandas as pd


def load_player_rates(leagues: list[str] | str = "Big 5 European Leagues Combined",
                      season: str | int = "2024-2025") -> pd.DataFrame:
    """Return ``[player, team, position, goal_rate, assist_rate]`` (per 90) from FBref."""
    try:
        import soccerdata as sd
    except Exception as e:  # pragma: no cover - optional dependency
        raise RuntimeError("soccerdata not installed; run `uv pip install -e '.[data]'`") from e

    fb = sd.FBref(leagues=leagues, seasons=season)
    std = fb.read_player_season_stats(stat_type="standard")
    df = std.reset_index()
    # FBref multi-index columns are flattened to tuples; pick robustly.
    nineties = _pick(df, ["90s", "Playing Time 90s"])
    npg = _pick(df, ["Per 90 Minutes npxG", "npG", "Performance npG", "Gls"])
    ast = _pick(df, ["Per 90 Minutes Ast", "Ast", "Performance Ast"])
    out = pd.DataFrame(
        {
            "player": _pick(df, ["player", "Player"]),
            "team": _pick(df, ["team", "Squad"]),
            "position": _pick(df, ["pos", "Pos"]).astype(str).str[:2],
        }
    )
    nineties = nineties.replace(0, pd.NA)
    out["goal_rate"] = (npg / nineties).fillna(0.0)
    out["assist_rate"] = (ast / nineties).fillna(0.0)
    return out


def _pick(df: pd.DataFrame, candidates: list[str]) -> pd.Series:
    for c in candidates:
        if c in df.columns:
            return df[c]
        for col in df.columns:
            if (isinstance(col, tuple) and c in col) or (isinstance(col, str) and col.endswith(c)):
                return df[col]
    return pd.Series([pd.NA] * len(df), index=df.index)
