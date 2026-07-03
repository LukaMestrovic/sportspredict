"""Backtest harness.

``run_backtest`` takes a frame of ``(fixture context, question, binary outcome)``,
runs the engine (simulating each fixture once), and reports the Brier decomposition overall
and per market family against a climatology baseline. It also writes reliability diagrams.

The same entry point serves the real backtest (StatsBomb-derived outcomes via
:mod:`sportspredict.ingest`) and an offline, well-specified **synthetic** check produced by
:func:`make_synthetic_dataset` — where ground truth is a single realized draw from the same
generative model, so a correct pipeline must come out calibrated (a strong sanity test of the
resolvers + metrics).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from ..config import Settings, default_settings
from ..engine import Engine
from ..features.context import MatchContext
from ..markets import parse_question, resolve
from ..rates import RateModel
from .brier import BrierDecomposition, brier_decomposition, brier_score
from .reliability import plot_reliability


@dataclass
class BacktestReport:
    overall: BrierDecomposition
    per_family: dict[str, BrierDecomposition]
    baselines: dict[str, float]
    n_matches: int
    n_questions: int

    def summary(self) -> str:
        lines = [
            f"matches={self.n_matches}  questions={self.n_questions}",
            f"overall Brier={self.overall.brier:.4f}  skill={self.overall.skill_score:+.3f}  "
            f"reliability={self.overall.reliability:.4f}  resolution={self.overall.resolution:.4f}  "
            f"uncertainty={self.overall.uncertainty:.4f}  ECE={self.overall.ece:.4f}",
            "baselines: " + "  ".join(f"{k}={v:.4f}" for k, v in self.baselines.items()),
            "per-family Brier / skill:",
        ]
        for fam, d in sorted(self.per_family.items()):
            lines.append(f"  {fam:24s} n={d.n:5d}  Brier={d.brier:.4f}  skill={d.skill_score:+.3f}")
        return "\n".join(lines)


def _ctx_from_row(row) -> MatchContext:
    return MatchContext(
        team_a=str(row["team_a"]),
        team_b=str(row["team_b"]),
        elo_a=float(row.get("elo_a", 1500.0)),
        elo_b=float(row.get("elo_b", 1500.0)),
        stage=str(row.get("stage", "group")),
        referee_card_mult=float(row.get("referee_card_mult", 1.0)),
        referee_foul_mult=float(row.get("referee_foul_mult", 1.0)),
        referee_pen_mult=float(row.get("referee_pen_mult", 1.0)),
    )


def _group_key(ctx: MatchContext) -> tuple:
    # Must cover every field that changes the simulation (mirrors Engine._simulate's key),
    # or rows with different referee multipliers would silently share one simulation.
    return (
        ctx.team_a, ctx.team_b, ctx.elo_a, ctx.elo_b, ctx.stage,
        ctx.referee_card_mult, ctx.referee_foul_mult, ctx.referee_pen_mult,
    )


def run_backtest(
    df: pd.DataFrame,
    engine: Engine | None = None,
    settings: Settings | None = None,
    out_dir: str | Path | None = None,
    n_sims: int | None = None,
    n_bins: int = 10,
) -> BacktestReport:
    settings = settings or default_settings()
    engine = engine or Engine(settings)
    rows = df.to_dict("records")

    # Group by fixture so each match simulates once.
    groups: dict[tuple, list[int]] = {}
    for i, r in enumerate(rows):
        ctx = _ctx_from_row(r)
        groups.setdefault(_group_key(ctx), []).append(i)

    p_final = np.empty(len(rows))
    p_model = np.empty(len(rows))
    families: list[str] = [""] * len(rows)
    outcomes = np.asarray([float(r["outcome"]) for r in rows])

    for key, idxs in groups.items():
        ctx = _ctx_from_row(rows[idxs[0]])
        qs = [str(rows[i]["question"]) for i in idxs]
        preds = engine.predict_many(ctx, qs, n_sims=n_sims)
        for i, pred in zip(idxs, preds):
            p_final[i] = pred.p_final
            p_model[i] = pred.p_model
            families[i] = pred.market

    overall = brier_decomposition(p_final, outcomes, n_bins)
    per_family = {
        fam: brier_decomposition(p_final[mask], outcomes[mask], n_bins)
        for fam in sorted(set(families))
        if (mask := np.array([f == fam for f in families])).sum() >= 1
    }

    # Baselines.
    climatology = np.full(len(rows), outcomes.mean())
    baselines = {
        "climatology": brier_score(climatology, outcomes),
        "model_only": brier_score(p_model, outcomes),
        "model_final": overall.brier,
    }

    if out_dir is not None:
        out = Path(out_dir)
        plot_reliability(p_final, outcomes, out / "reliability_overall.png", "Overall reliability", n_bins)
        for fam, d in per_family.items():
            mask = np.array([f == fam for f in families])
            if mask.sum() >= 20:
                plot_reliability(
                    p_final[mask], outcomes[mask], out / f"reliability_{fam}.png", f"Reliability: {fam}", n_bins
                )

    return BacktestReport(
        overall=overall,
        per_family=per_family,
        baselines=baselines,
        n_matches=len(groups),
        n_questions=len(rows),
    )


# -- synthetic, well-specified dataset for offline validation ---------------
_TEAM_NAMES = [
    "Alphaland", "Bravoria", "Charlieo", "Deltania", "Echostan", "Foxtronia",
    "Golfland", "Hotelia", "Indiabad", "Julietto", "Kiloland", "Limaria",
    "Mikeland", "Novembia", "Oscarro", "Papaland", "Quebecia", "Romeoland",
]

_QUESTION_TEMPLATES = [
    "Will {A} win?",
    "Will both teams score?",
    "Will there be under 2.5 goals?",
    "Will there be 2 or more offsides in the match?",
    "Will {A} commit more fouls than {B}?",
    "Will {A} have more second-half corners than {B}?",
    "Will there be a penalty or a red card?",
    "Will there be a goal in the first half?",
    "Will {A} score in both halves?",
]


def make_synthetic_dataset(
    n_matches: int = 200,
    seed: int = 11,
    settings: Settings | None = None,
) -> pd.DataFrame:
    """Generate fixtures with ground truth = one realized draw from the generative model.

    Truth for each question is a Bernoulli sample with probability equal to the market
    resolved on a single realized match, which makes a correct predictor calibrated.
    """
    settings = settings or default_settings()
    rm = RateModel(settings)
    rng = np.random.default_rng(seed)
    sim_rng = np.random.default_rng(seed + 999)
    from ..model import simulate

    records = []
    for m in range(n_matches):
        a, b = rng.choice(len(_TEAM_NAMES), size=2, replace=False)
        team_a, team_b = _TEAM_NAMES[a], _TEAM_NAMES[b]
        elo_a = float(rng.normal(1700, 150))
        elo_b = float(rng.normal(1700, 150))
        stage = "group" if rng.random() < 0.7 else "knockout"
        ctx = MatchContext(team_a, team_b, elo_a=elo_a, elo_b=elo_b, stage=stage)
        rates = rm.build(ctx)
        truth = simulate(rates, n_sims=1, rng=sim_rng, settings=settings)  # one realized match

        for tmpl in _QUESTION_TEMPLATES:
            q = tmpl.format(A=team_a, B=team_b)
            spec = parse_question(q, ctx)
            p_truth = resolve(spec, truth, ctx, settings)  # 0/1 for most; prob for player/ties
            outcome = int(sim_rng.random() < p_truth)
            records.append(
                {
                    "team_a": team_a, "team_b": team_b, "elo_a": elo_a, "elo_b": elo_b,
                    "stage": stage, "question": q, "outcome": outcome,
                }
            )
    return pd.DataFrame.from_records(records)


def _main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(description="Backtest (synthetic by default, or a CSV).")
    ap.add_argument("--input", default=None, help="CSV with columns incl. question,outcome")
    ap.add_argument("--matches", type=int, default=300, help="synthetic match count")
    ap.add_argument("--n-sims", type=int, default=20000)
    ap.add_argument("--out-dir", default="data/processed/validation")
    args = ap.parse_args(argv)

    df = pd.read_csv(args.input) if args.input else make_synthetic_dataset(n_matches=args.matches)
    report = run_backtest(df, n_sims=args.n_sims, out_dir=args.out_dir)
    print(report.summary())
    print(f"\nreliability diagrams -> {args.out_dir}/")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
