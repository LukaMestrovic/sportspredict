"""Fit per-(player, stat) shares of the team total, shrunk toward position priors.

Consumes ``player_stat_table.parquet`` and writes ``player_shares.json`` keyed
``(player, stat)`` with columns ``player, team, stat, share`` for the runtime
``postsim.allocation.PlayerShares`` loader.

Shots on target and goals are fit. A player's per-90 rate is shrunk toward the position-prior lambda by an effective-matches
weight, then divided by the canonical team lambda so it is a *share of the team total* on the same
scale as ``position_prior_share`` — i.e. a player with no history reduces exactly to the position
prior, so the allocation layer degrades gracefully for unseen players. Only ``reconciles_sot`` rows
(summed player SoT == team SoT) are trusted, the filter the feasibility memo requires.
"""

from __future__ import annotations

import argparse
import json
import unicodedata
from pathlib import Path

import pandas as pd

from sportspredict.config import Settings, default_settings

# Player-stat column -> the config key under player_stat_lambda.
_STAT_COL = {"shots_on_target": "shots_on", "goals": "goals", "assists": "assists"}


def _fold(s: str) -> str:
    s = (s or "").translate(str.maketrans({"Ø": "O", "ø": "o", "Ł": "L", "ł": "l",
                                            "Đ": "D", "đ": "d", "Þ": "Th", "þ": "th"}))
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower().strip()


def _team_lambda_canon(settings: Settings, stat: str) -> tuple[dict[str, float], float]:
    """Position-prior lambdas and the canonical-XI team total (the share denominator)."""
    if stat == "goals":
        prior = settings.raw.get("players", {}).get("default_goal_rate", {})
    elif stat == "assists":
        prior = settings.raw.get("players", {}).get("default_assist_rate", {})
    else:
        prior = settings.raw.get("player_stat_lambda", {}).get(stat, {})
    formation = (settings.raw.get("players", {}) or {}).get(
        "canonical_formation", {"GK": 1, "DF": 4, "MF": 4, "FW": 2})
    fallback = float(prior.get("FW", 0.0))
    team = sum(int(n) * float(prior.get(pos, fallback)) for pos, n in formation.items())
    return {k: float(v) for k, v in prior.items()}, float(team)


def fit_shares(players: pd.DataFrame, settings: Settings | None = None, *,
               stat: str = "shots_on_target", k: float = 4.0) -> pd.DataFrame:
    """Fit shrunk per-(player) shares of the team total for ``stat`` (default shots on target)."""
    settings = settings or default_settings()
    col = _STAT_COL[stat]
    prior_lambda, team_lambda = _team_lambda_canon(settings, stat)
    fallback = prior_lambda.get("FW", 0.0)

    valid = players["minutes"] > 0
    if stat == "shots_on_target":
        valid &= players["reconciles_sot"]
    df = players[valid].copy()
    df["player_key"] = df["player"].map(_fold)
    rows = []
    for key, g in df.groupby("player_key"):
        if not key:
            continue
        mins = float(g["minutes"].sum())
        if mins <= 0:
            continue
        cnt = float(g[col].sum())
        per90 = cnt / mins * 90.0
        pos = g["position"].mode().iloc[0] if not g["position"].mode().empty else "FW"
        n_eff = mins / 90.0
        prior90 = prior_lambda.get(pos, fallback)
        per90_hat = (n_eff * per90 + k * prior90) / (n_eff + k)
        rows.append({
            "player": g["player"].iloc[0],
            "team": g["team"].mode().iloc[0] if "team" in g and not g["team"].mode().empty else None,
            "stat": stat,
            "share": per90_hat / team_lambda if team_lambda > 0 else 0.0,
            "per90": per90,
            "n_app": int(len(g)),
            "position": pos,
        })
    return pd.DataFrame(rows)


def write_shares(shares: pd.DataFrame, path: str | Path) -> None:
    """Write fitted shares in the compact runtime format, or Parquet when requested."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.suffix == ".json":
        cols = ["player", "team", "stat", "share"]
        rows = []
        for row in shares[cols].itertuples(index=False):
            rows.append([
                str(row.player),
                None if pd.isna(row.team) else str(row.team),
                str(row.stat),
                float(row.share),
            ])
        payload = {"schema_version": 1, "columns": cols, "rows": rows}
        path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return
    shares.to_parquet(path)


def _main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Fit player_shares from player_stat_table.parquet.")
    ap.add_argument("--players", default="data/processed/player_stat_table.parquet")
    ap.add_argument("--out", default="data/processed/player_shares.json")
    ap.add_argument("--k", type=float, default=4.0, help="shrinkage strength (effective matches)")
    ap.add_argument("--stat", choices=["all", *_STAT_COL], default="all")
    args = ap.parse_args(argv)
    settings = default_settings()
    players = pd.read_parquet(settings.path(args.players))
    stats = list(_STAT_COL) if args.stat == "all" else [args.stat]
    shares = pd.concat([fit_shares(players, settings, stat=stat, k=args.k) for stat in stats],
                       ignore_index=True)
    out_path = settings.path(args.out)
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    write_shares(shares, out_path)
    print(f"[shares] fit {len(shares)} players (k={args.k}) -> {out_path}")
    print(shares.sort_values('per90', ascending=False).head(10).to_string(index=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
