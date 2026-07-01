"""Offline Brier backtest of the player-allocation share model vs the flat position prior.

This is the gate that decides whether ``postsim.player_allocation`` may be turned on. For each
held-out tournament we fit
shares on every *other* match (leave-one-tournament-out, so a player's share never peeks at the
fixture being scored), simulate each held fixture, and price ``P(starter gets >= k shots on target)``
two ways on the **same simulated team total** (so only the share differs):

* **prior**  — the position-prior share, and
* **share**  — the fitted per-player share (falls back to the prior for unseen players).

Both go through the production ``allocate_player_prob`` (Binomial-on-team-total), and are scored by
Brier against the real per-player outcome from ``player_stat_table`` (reconciling team-sides only).
The same folds also score the aggregate any-player 2+ SoT, any-player brace, and substitute-scorer
questions through their production resolvers. If the share model does not beat the prior here, the
layer stays off — exactly like the referee features.
"""

from __future__ import annotations

import argparse
import json

import numpy as np
import pandas as pd

from sportspredict.config import Settings, default_settings
from sportspredict.features.context import MatchContext, PlayerInfo
from sportspredict.ingest.elo import load_elo_table
from sportspredict.model import simulate

from ..rates import make_rate_model
from ..rates.ingest_apifootball import Canonicalizer
from .allocation import (
    PlayerShares,
    allocate_player_prob,
    position_prior_share,
    prob_any_player_threshold,
    prob_substitute_scores,
)
from .fit_shares import _fold, fit_shares

_STAT = "shots_on_target"


def _brier(p: np.ndarray, y: np.ndarray) -> float:
    return float(np.mean((p - y) ** 2))


def _share_map(shares: pd.DataFrame) -> dict[str, float]:
    return {_fold(r.player): float(r.share) for r in shares.itertuples()}


def evaluate(settings: Settings, *, elo_csv: str, n_sims: int, held: list[str] | None,
             thresholds=(1, 2), k: float = 4.0) -> dict:
    players = pd.read_parquet(settings.path("data/processed/player_stat_table.parquet"))
    history = pd.read_parquet(settings.path("data/processed/history_stat_table.parquet"))
    if "source" in history:
        history = history[history["source"] == "apifootball"].copy()
    else:
        history = history[history["match_id"].isin(set(players["match_id"]))].copy()
    elo = load_elo_table(settings.path(elo_csv))
    canon = Canonicalizer(elo)
    model = make_rate_model(settings)
    seed = int(settings.seed) if settings.seed is not None else 0

    all_t = sorted(players["tournament"].unique())
    folds = [t for t in all_t if t in set(held)] if held else all_t
    # accumulators: per threshold -> {"prior": [se...], "share": [...]}
    acc = {k_: {"prior": [], "share": [], "y": []} for k_ in thresholds}
    aggregate = {
        "any_player_2plus_sot": {"prior": [], "share": [], "y": []},
        "any_player_brace": {"prior": [], "share": [], "y": []},
        "substitute_scores": {"prior": [], "share": [], "y": []},
    }
    n_players = 0

    for t in folds:
        training = players[players["tournament"] != t]
        shares = fit_shares(training, settings, k=k)
        goal_shares = fit_shares(training, settings, stat="goals", k=k)
        smap = _share_map(shares)
        sot_model = PlayerShares({
            (_fold(r.player), "shots_on_target"): float(r.share) for r in shares.itertuples()
        })
        goal_model = PlayerShares({
            (_fold(r.player), "goals"): float(r.share) for r in goal_shares.itertuples()
        })
        hist_t = history[history["tournament"] == t]
        for hrow in hist_t.itertuples(index=False):
            mid = int(hrow.match_id)
            all_pl = players[(players["match_id"] == mid) & (players["minutes"] > 0)]
            pl = all_pl[(all_pl["reconciles_sot"]) & (~all_pl["substitute"])]
            if pl.empty:
                continue
            ctx = MatchContext(hrow.home_team, hrow.away_team,
                               elo_a=float(getattr(hrow, "home_elo", 1500.0)),
                               elo_b=float(getattr(hrow, "away_elo", 1500.0)),
                               stage=str(getattr(hrow, "stage", "group")))
            ctx.lineup_a = [
                PlayerInfo(str(p.player), ctx.team_a, str(p.position),
                           start_prob=0.2 if p.substitute else 1.0,
                           expected_minutes=float(p.minutes))
                for p in all_pl[all_pl["team_side"] == "home"].itertuples()
            ]
            ctx.lineup_b = [
                PlayerInfo(str(p.player), ctx.team_b, str(p.position),
                           start_prob=0.2 if p.substitute else 1.0,
                           expected_minutes=float(p.minutes))
                for p in all_pl[all_pl["team_side"] == "away"].itertuples()
            ]
            rates = model.build(ctx)
            out = simulate(rates, n_sims=n_sims, rng=np.random.default_rng(seed), settings=settings)
            for prow in pl.itertuples(index=False):
                team_idx = 0 if prow.team_side == "home" else 1
                pos = prow.position
                s_prior = position_prior_share(pos, _STAT, settings)
                if s_prior is None:
                    continue
                s_fit = smap.get(_fold(prow.player), s_prior)
                n_players += 1
                for thr in thresholds:
                    p_prior = allocate_player_prob(out, team_idx, _STAT, s_prior, 1.0, ">=", thr)
                    p_share = allocate_player_prob(out, team_idx, _STAT, s_fit, 1.0, ">=", thr)
                    y = float(prow.shots_on >= thr)
                    acc[thr]["prior"].append((p_prior - y) ** 2)
                    acc[thr]["share"].append((p_share - y) ** 2)
                    acc[thr]["y"].append(y)

            any_sot_y = float((all_pl["shots_on"] >= 2).any())
            any_sot_prior = prob_any_player_threshold(
                out, ctx, "shots_on_target", ">=", 2, None, settings
            )
            any_sot_share = prob_any_player_threshold(
                out, ctx, "shots_on_target", ">=", 2, sot_model, settings
            )
            brace_y = float((all_pl["goals"] >= 2).any())
            brace_prior = prob_any_player_threshold(out, ctx, "goals", ">=", 2, None, settings)
            brace_share = prob_any_player_threshold(
                out, ctx, "goals", ">=", 2, goal_model, settings
            )
            sub_y = float(((all_pl["substitute"]) & (all_pl["goals"] >= 1)).any())
            sub_prior = prob_substitute_scores(
                out, ctx, None, settings, fallback_share=0.12
            )
            sub_share = prob_substitute_scores(
                out, ctx, goal_model, settings, fallback_share=0.12
            )
            for name, prior_p, share_p, y in (
                ("any_player_2plus_sot", any_sot_prior, any_sot_share, any_sot_y),
                ("any_player_brace", brace_prior, brace_share, brace_y),
                ("substitute_scores", sub_prior, sub_share, sub_y),
            ):
                aggregate[name]["prior"].append((prior_p - y) ** 2)
                aggregate[name]["share"].append((share_p - y) ** 2)
                aggregate[name]["y"].append(y)

    res = {"n_player_questions": n_players, "tournaments": folds, "k": k, "by_threshold": {}}
    for thr in thresholds:
        prior = np.array(acc[thr]["prior"]); share = np.array(acc[thr]["share"]); y = np.array(acc[thr]["y"])
        res["by_threshold"][f">={thr}"] = {
            "n": int(len(y)), "label_mean": float(y.mean()) if len(y) else 0.0,
            "prior_brier": float(prior.mean()) if len(prior) else 0.0,
            "share_brier": float(share.mean()) if len(share) else 0.0,
            "delta_share_minus_prior": float(share.mean() - prior.mean()) if len(y) else 0.0,
        }
    res["aggregate_markets"] = {}
    for name, values in aggregate.items():
        prior = np.asarray(values["prior"]); share = np.asarray(values["share"])
        y = np.asarray(values["y"])
        res["aggregate_markets"][name] = {
            "n": int(len(y)), "label_mean": float(y.mean()) if len(y) else 0.0,
            "prior_brier": float(prior.mean()) if len(y) else 0.0,
            "share_brier": float(share.mean()) if len(y) else 0.0,
            "delta_share_minus_prior": float(share.mean() - prior.mean()) if len(y) else 0.0,
        }
    return res


def _print(out: dict) -> None:
    print(f"=== PLAYER-PROP BRIER: share vs position prior (LOTO) ===")
    print(f"tournaments={','.join(out['tournaments'])}  player-questions={out['n_player_questions']}  k={out['k']}")
    print(f"\n{'prop':8s} {'n':>6s} {'label':>7s} {'prior':>9s} {'share':>9s} {'delta':>9s}")
    print("-" * 52)
    for prop, d in out["by_threshold"].items():
        verdict = "share better" if d["delta_share_minus_prior"] < 0 else "prior better" if d["delta_share_minus_prior"] > 0 else "tied"
        print(f"{prop+' SoT':8s} {d['n']:6d} {d['label_mean']:7.3f} {d['prior_brier']:9.5f} "
              f"{d['share_brier']:9.5f} {d['delta_share_minus_prior']:+9.5f}  [{verdict}]")
    for prop, d in out.get("aggregate_markets", {}).items():
        verdict = "share better" if d["delta_share_minus_prior"] < 0 else "prior better"
        print(f"{prop:22s} n={d['n']:5d} label={d['label_mean']:.3f} "
              f"prior={d['prior_brier']:.5f} share={d['share_brier']:.5f} "
              f"delta={d['delta_share_minus_prior']:+.5f} [{verdict}]")


def _main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Backtest player-prop share model vs the position prior.")
    ap.add_argument("--elo-csv", dest="elo_csv", default="data/raw/elo.csv")
    ap.add_argument("--n-sims", dest="n_sims", type=int, default=3000)
    ap.add_argument("--held-tournaments", dest="held", default=None,
                    help="comma-separated tournaments to score as held-out folds (default: all)")
    ap.add_argument("--k", type=float, default=4.0)
    ap.add_argument("--json", dest="as_json", action="store_true")
    args = ap.parse_args(argv)
    settings = default_settings()
    held = [t.strip() for t in args.held.split(",")] if args.held else None
    out = evaluate(settings, elo_csv=args.elo_csv, n_sims=args.n_sims, held=held, k=args.k)
    print(json.dumps(out, indent=2)) if args.as_json else _print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
