"""Offline, reproducible Brier backtest for comparing rate models.

The only sound, network-free comparison we have is **leave-one-tournament-out** (LOTO) over the
shipped historical table (`data/processed/history_stat_table.parquet`). Each match expands into a
set of team-level questions resolved against its true per-half stats; for each held-out tournament
we train the learned model on the other tournaments and score Brier out-of-sample.

What this measures and what it does not:
* It compares the **learned Layer-1 rates** against the **baseline** `RateModel`, per market family.
* It does NOT measure **API-Football lineup enrichment**: the question set is team-level only and
  excludes every player market, so lineups cannot move a single number here.

CLI: ``sphybrid backtest`` (LOTO) / ``sphybrid backtest --in-sample`` (faster single pass).
"""

from __future__ import annotations

import argparse
import copy
import json
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd

from sportspredict.config import Settings, default_settings
from sportspredict.engine import Engine
from sportspredict.features.context import MatchContext
from sportspredict.markets import parse_question, resolve
from sportspredict.markets.schema import MarketType
from sportspredict.model.outcome import MatchOutcome
from sportspredict.rates import RateModel
from sportspredict.types import (
    GOALS, H1, H2, PER_HALF_STATS, RESULT_A, RESULT_B, RESULT_DRAW, TEAM_A, TEAM_B,
)
from sportspredict.validation.backtest import run_backtest

from .engine import build_engine

# Player and shootout/penalty markets are excluded: the StatsBomb table has no lineups, and the
# penalty/red labels are unreliable.
_EXCLUDED_MARKETS = {
    MarketType.PLAYER_SCORE, MarketType.PLAYER_SCORE_OR_ASSIST, MarketType.PLAYER_STAT,
    MarketType.GOES_TO_ET, MarketType.GOES_TO_SHOOTOUT,
    MarketType.PENALTY_AWARDED, MarketType.PENALTY_OR_RED,
}


def build_match_outcome(row: dict) -> MatchOutcome:
    """A single-sim MatchOutcome carrying the match's true per-half counts (the settled label)."""
    reg, et = {}, {}
    for stat in PER_HALF_STATS:
        a = np.zeros((2, 2, 1), dtype=float)
        for team, side in ((TEAM_A, "home"), (TEAM_B, "away")):
            a[team, H1, 0] = float(row[f"{side}_{stat}_h1"])
            a[team, H2, 0] = float(row[f"{side}_{stat}_h2"])
        reg[stat] = a
        et[stat] = np.zeros((2, 1), dtype=float)
    ga, gb = reg[GOALS][TEAM_A, :, 0].sum(), reg[GOALS][TEAM_B, :, 0].sum()
    result = RESULT_A if ga > gb else RESULT_B if gb > ga else RESULT_DRAW
    return MatchOutcome(
        n_sims=1, reg_counts=reg, et_counts=et,
        reds=np.array([[float(row["home_reds"])], [float(row["away_reds"])]], dtype=float),
        penalties=np.array([float(row.get("penalties", 0))], dtype=float),
        et_played=np.array([False]), result=np.array([result]),
        gamma_tempo=np.ones(1), gamma_phys=np.ones(1),
    )


def match_questions(a: str, b: str) -> list[str]:
    """The team-level question set scored for every historical match (no player markets)."""
    return [
        f"Will {a} win?", f"Will {b} win?", "Will the match be a draw?",
        "Will both teams score?", "Will there be over 1.5 goals?",
        "Will there be over 2.5 goals?", "Will there be under 2.5 goals?",
        "Will there be over 3.5 goals?",
        f"Will {a} have more corners than {b}?", f"Will {b} have more corners than {a}?",
        f"Will {a} commit more fouls than {b}?",
        f"Will {a} have more shots on target than {b}?",
        f"Will {a} have more second-half corners than {b}?",
        "Will there be at least 9 corners?", "Will there be at least 3 yellow cards?",
        "Will there be over 23 fouls?",
        f"Will {a} have at least 5 corners?", f"Will {b} have at least 5 corners?",
        f"Will {a} score 2 or more goals?", f"Will {b} score 2 or more goals?",
        "Will there be a goal in the first half?", "Will there be a goal in the second half?",
        f"Will {a} score in both halves?",
        "Will more goals be scored in the second half than the first?",
        f"Will {a} win to nil?", f"Will {b} win to nil?",
        f"Will {a} keep a clean sheet?", f"Will {b} keep a clean sheet?",
        "Will an odd number of goals be scored?",
        "Will both teams score and there be over 2.5 goals?",
    ]


def offsides_questions(a: str, b: str) -> list[str]:
    """Questions scored only on correctly-counted API-Football rows."""
    return [
        "Will there be over 2.5 offsides?", "Will there be over 3.5 offsides?",
        f"Will {a} have more offsides than {b}?", f"Will {b} have more offsides than {a}?",
        f"Will {a} have at least 2 offsides?", f"Will {b} have at least 2 offsides?",
    ]


def _resolve_for_match(ctx: MatchContext, outcome: MatchOutcome, questions: list[str],
                       settings: Settings) -> list[tuple[str, str, int]]:
    rows = []
    for q in questions:
        try:
            spec = parse_question(q, ctx)
            if spec.market in _EXCLUDED_MARKETS:
                continue
            rows.append((q, spec.family, int(round(float(resolve(spec, outcome, ctx, settings))))))
        except Exception:
            continue
    return rows


def history_dataset(table: pd.DataFrame, settings: Settings) -> pd.DataFrame:
    """Expand the match stat table into a question-level dataset with settled outcomes.

    Columns: team_a, team_b, elo_a, elo_b, stage, tournament, question, family, outcome.
    """
    records = []
    for row in table.to_dict("records"):
        a, b = str(row["home_team"]), str(row["away_team"])
        ctx = MatchContext(a, b, elo_a=float(row.get("home_elo", 1500.0)),
                           elo_b=float(row.get("away_elo", 1500.0)),
                           stage=str(row.get("stage", "group")))
        outcome = build_match_outcome(row)
        questions = match_questions(a, b)
        if str(row.get("source", "statsbomb")) == "apifootball":
            questions += offsides_questions(a, b)
        for q, family, label in _resolve_for_match(ctx, outcome, questions, settings):
            records.append({
                "team_a": a, "team_b": b, "elo_a": ctx.elo_a, "elo_b": ctx.elo_b,
                "stage": ctx.stage, "tournament": str(row.get("tournament", "all")),
                "question": q, "family": family, "outcome": label,
            })
    return pd.DataFrame.from_records(records)


def _pool(reports: list) -> dict:
    """n-weighted pooling of Brier across folds, overall and per market family."""
    total = sum(r.overall.n for r in reports) or 1
    fam_n, fam_sse = {}, {}
    for r in reports:
        for fam, d in r.per_family.items():
            fam_n[fam] = fam_n.get(fam, 0) + d.n
            fam_sse[fam] = fam_sse.get(fam, 0.0) + d.n * d.brier
    return {
        "n": int(total),
        "brier": float(sum(r.overall.n * r.overall.brier for r in reports) / total),
        "per_family": {f: {"n": fam_n[f], "brier": fam_sse[f] / fam_n[f]}
                       for f in sorted(fam_n) if fam_n[f]},
    }


def _baseline_engine(settings: Settings) -> Engine:
    return Engine(settings=settings, rate_model=RateModel(settings))


def _fold_engine(train_table: pd.DataFrame, settings: Settings) -> Engine:
    """Train the learned model on ``train_table`` and return an Engine using it (out-of-sample)."""
    from .rates.assemble import results_from_stat_table
    from .rates.learned import LearnedRateModel
    from .rates.team_ratings import fit_team_ratings
    from .rates.train import train_rate_models

    tmp = Path(tempfile.mkdtemp(prefix="loto_"))
    ratings = fit_team_ratings(results_from_stat_table(train_table))
    ratings_path = tmp / "team_ratings.parquet"
    ratings.save(ratings_path)
    artifact = tmp / "rate_model.joblib"
    train_rate_models(train_table, ratings, settings, artifact_path=artifact,
                      metadata_path=tmp / "rate_model.json")
    raw = copy.deepcopy(settings.raw)
    raw.setdefault("rates", {}).setdefault("learned", {})
    raw["rates"]["model"] = "learned"
    raw["rates"]["learned"].update({"artifact": str(artifact), "team_ratings": str(ratings_path)})
    s2 = Settings(raw=raw, market_rules=settings.market_rules, root=settings.root)
    model = LearnedRateModel.from_settings(s2)
    if model is None:  # pragma: no cover - implies a broken training run
        raise RuntimeError("fold training did not produce a usable LearnedRateModel")
    return Engine(settings=settings, rate_model=model)


def loto_compare(table: pd.DataFrame, settings: Settings, *, n_sims: int,
                 held: list[str] | None = None) -> dict:
    """Leave-one-tournament-out Brier for baseline vs the learned model, pooled across folds.

    ``held`` restricts which tournaments are *scored* as held-out folds (each still trains the
    learned model on every other match). With the API-Football-expanded table this keeps the
    comparison tractable: hold out only the trusted StatsBomb tournaments, train on everything else.
    """
    base_reports, learned_reports = [], []
    all_tournaments = sorted(table["tournament"].unique())
    tournaments = [t for t in all_tournaments if t in set(held)] if held else all_tournaments
    for t in tournaments:
        train = table[table["tournament"] != t]
        held = table[table["tournament"] == t]
        held_df = history_dataset(held, settings)
        base_reports.append(run_backtest(held_df, engine=_baseline_engine(settings),
                                         settings=settings, n_sims=n_sims))
        learned_reports.append(run_backtest(held_df, engine=_fold_engine(train, settings),
                                            settings=settings, n_sims=n_sims))
    base = _pool(base_reports)
    learned = _pool(learned_reports)
    return _comparison("loto", tournaments, base, learned)


def in_sample_compare(table: pd.DataFrame, settings: Settings, *, n_sims: int) -> dict:
    """Single-pass baseline vs the active (already-trained) engine. Optimistic — for a quick check."""
    df = history_dataset(table, settings)
    base = _pool([run_backtest(df, engine=_baseline_engine(settings), settings=settings, n_sims=n_sims)])
    learned = _pool([run_backtest(df, engine=build_engine(settings), settings=settings, n_sims=n_sims)])
    return _comparison("in_sample", sorted(table["tournament"].unique()), base, learned)


def _comparison(mode: str, tournaments: list[str], base: dict, learned: dict) -> dict:
    families = sorted(set(base["per_family"]) | set(learned["per_family"]))
    return {
        "mode": mode,
        "tournaments": list(tournaments),
        "n_questions": base["n"],
        "baseline": base,
        "learned": learned,
        "delta_brier_learned_minus_baseline": learned["brier"] - base["brier"],
        "per_family": {
            f: {
                "baseline": base["per_family"].get(f, {}).get("brier"),
                "learned": learned["per_family"].get(f, {}).get("brier"),
                "n": learned["per_family"].get(f, base["per_family"].get(f, {})).get("n"),
            }
            for f in families
        },
        "scoring": {"brier": "(p - outcome)^2",
                    "note": "lineup enrichment is not measured here"},
    }


def _print(out: dict) -> None:
    print(f"=== {out['mode'].upper()} BRIER: baseline vs learned ===")
    print(f"tournaments={','.join(out['tournaments'])}  questions={out['n_questions']}")
    print(f"\n{'model':10s} {'n':>6s}  {'Brier':>8s}")
    print("-" * 28)
    print(f"{'baseline':10s} {out['baseline']['n']:6d}  {out['baseline']['brier']:.5f}")
    print(f"{'learned':10s} {out['learned']['n']:6d}  {out['learned']['brier']:.5f}")
    d = out["delta_brier_learned_minus_baseline"]
    verdict = "learned better" if d < 0 else "baseline better" if d > 0 else "tied"
    print(f"\ndelta (learned - baseline) = {d:+.5f}   [{verdict}]")
    print(f"\n{'family':28s} {'n':>5s}  {'baseline':>9s}  {'learned':>9s}  {'delta':>8s}")
    print("-" * 66)
    for fam, d in out["per_family"].items():
        b, l = d.get("baseline"), d.get("learned")
        if b is None or l is None:
            continue
        print(f"{fam:28s} {d['n']:5d}  {b:9.5f}  {l:9.5f}  {l - b:+8.5f}")


def _main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Offline Brier backtest: baseline vs learned rates.")
    ap.add_argument("--stat-table", default="data/processed/history_stat_table.parquet")
    ap.add_argument("--n-sims", dest="n_sims", type=int, default=4000)
    ap.add_argument("--in-sample", action="store_true",
                    help="single optimistic pass instead of leave-one-tournament-out")
    ap.add_argument("--held-tournaments", dest="held", default=None,
                    help="comma-separated tournaments to score as held-out folds (LOTO trains on the "
                         "rest). Default: all. Use to keep the expanded-table LOTO tractable.")
    ap.add_argument("--learned-stats", dest="learned_stats", default=None,
                    help="override learned stats (comma-separated), useful for an ablation")
    ap.add_argument("--json", dest="as_json", action="store_true")
    args = ap.parse_args(argv)

    settings = default_settings()
    if args.learned_stats:
        settings.raw["rates"]["learned"]["learned_stats"] = [
            stat.strip() for stat in args.learned_stats.split(",") if stat.strip()
        ]
    table = pd.read_parquet(settings.path(args.stat_table))
    if "tournament" not in table.columns:
        table = table.assign(tournament="all")
    held = [t.strip() for t in args.held.split(",")] if args.held else None
    out = (in_sample_compare(table, settings, n_sims=args.n_sims) if args.in_sample
           else loto_compare(table, settings, n_sims=args.n_sims, held=held))
    if args.as_json:
        print(json.dumps(out, indent=2))
    else:
        _print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
