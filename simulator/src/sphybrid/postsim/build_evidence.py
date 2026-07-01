"""Build the compact historical-performance artifact consumed by simulation-report."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd

from sportspredict.config import Settings, default_settings
from sportspredict.features.context import MatchContext

from ..engine import build_engine
from .backtest_exotics import (
    _settings_from_fold_artifacts,
    point_in_time_elos,
    tournament_empirical_rates,
)
from .catalog import catalog_teams
from .contracts import contract_key


def _performance(group: pd.DataFrame, *, probability: str, fold: str | None = None) -> dict:
    if group.empty:
        return {"available": False, "reason": "No unseen settled questions for this contract."}
    y = group["outcome"].astype(float).to_numpy()
    p = group[probability].astype(float).to_numpy()
    result = {
        "available": True,
        "questions": int(len(group)),
        "matches": int(group["match_id"].nunique()),
        "brier": round(float(np.mean((p - y) ** 2)), 6),
        "always_50_brier": 0.25,
    }
    result["delta_vs_always_50"] = round(result["brier"] - 0.25, 6)
    if fold and fold in group:
        result["test_folds"] = sorted({int(value) for value in group[fold].dropna()})
    return result


def _empirical(group: pd.DataFrame) -> dict:
    if group.empty:
        return {"available": False, "reason": "No exact historical labels for this contract."}
    if "outcome" not in group:
        observations = int(group["n_all"].sum())
        yes_events = int(round(float((group["n_all"] * group["empirical_rate"]).sum())))
        return {
            "available": True,
            "yes_events": yes_events,
            "observations": observations,
            "matches": int(group["matches_all"].sum()),
            "rate": round(yes_events / observations, 6) if observations else 0.0,
        }
    outcomes = group["outcome"].astype(int)
    return {
        "available": True,
        "yes_events": int(outcomes.sum()),
        "observations": int(len(outcomes)),
        "matches": int(group["match_id"].nunique()),
        "rate": round(float(outcomes.mean()), 6),
    }


def build_evidence_artifact(
    oos_rows: pd.DataFrame,
    empirical_rows: pd.DataFrame,
    wc2026_rows: pd.DataFrame,
    wc2026_empirical: pd.DataFrame | None = None,
) -> dict:
    """Combine leakage-safe model scores and observed rates by exact contract."""
    keys = sorted(
        set(oos_rows.get("contract_key", []))
        | set(empirical_rows.get("contract_key", []))
        | set(wc2026_rows.get("contract_key", []))
        | set((wc2026_empirical if wc2026_empirical is not None else pd.DataFrame()).get(
            "contract_key", []
        ))
    )
    contracts = {}
    for key in keys:
        oos = oos_rows[oos_rows.contract_key == key]
        empirical = empirical_rows[empirical_rows.contract_key == key]
        wc = wc2026_rows[wc2026_rows.contract_key == key]
        wc_all = (
            wc2026_empirical[wc2026_empirical.contract_key == key]
            if wc2026_empirical is not None else pd.DataFrame()
        )
        wc_empirical = _empirical(wc_all if not wc_all.empty else wc)
        if wc_empirical.get("available"):
            wc_empirical["basis"] = (
                "all exact-labelable matches in the shipped WC2026 stat/event data"
                if not wc_all.empty else "settled SportPredict question instances"
            )
            if not wc_all.empty:
                wc_empirical["data_matches"] = int(wc_all.data_matches.max())
                wc_empirical["data_through"] = str(wc_all.data_through.max())
        contracts[str(key)] = {
            "contract_key": str(key),
            "model_performance": {
                "all_history": _performance(oos, probability="p_model", fold="fold_year"),
                "wc2026": _performance(wc, probability="p_model"),
            },
            "empirical_rate": {
                "all_history": _empirical(empirical),
                "wc2026": wc_empirical,
            },
        }
    return {
        "schema_version": 1,
        "methodology": {
            "all_history_performance": (
                "Rolling-origin calendar folds; every test match is later than all model-fitting data."
            ),
            "wc2026_performance": (
                "Settled SportPredict questions priced by artifacts fitted only through 2025."
            ),
            "empirical_rates": (
                "Observed YES count divided by exact labelable instances; WC2026 prefers every "
                "labelable match in the shipped tournament data and otherwise uses settled questions."
            ),
        },
        "contracts": contracts,
    }


def _pre_year_elos(history: pd.DataFrame, year: int) -> dict[str, float]:
    """End-of-prior-year ratings using the same causal Elo updates as the rolling backtest."""
    prior = history[pd.to_datetime(history.match_date).dt.year < year].copy()
    # Append one scoreless sentinel per team after the final real date. Its pre-match Elo is the
    # desired snapshot; no sentinel result is ever used.
    teams = sorted(set(prior.home_team.astype(str)) | set(prior.away_team.astype(str)))
    if not teams:
        return {}
    final_date = pd.Timestamp(f"{year}-01-01")
    sentinels = []
    for index, team in enumerate(teams):
        row = prior.iloc[-1].copy()
        row["match_id"] = -index - 1
        row["source"] = "snapshot"
        row["match_date"] = final_date
        row["home_team"], row["away_team"] = team, f"__snapshot_{index}"
        row["home_goals_h1"] = row["home_goals_h2"] = 0
        row["away_goals_h1"] = row["away_goals_h2"] = 0
        sentinels.append(row)
    extended = pd.concat([prior, pd.DataFrame(sentinels)], ignore_index=True)
    rated = point_in_time_elos(extended)
    snapshot = rated[rated.source.astype(str) == "snapshot"]
    return {str(row.home_team): float(row.home_elo) for row in snapshot.itertuples()}


def score_settled_catalog(
    settings: Settings | None = None,
    *,
    catalog_path: str | Path = "data/processed/sportspredict_question_catalog.csv",
    fold_artifacts: str | Path,
    n_sims: int = 1000,
) -> pd.DataFrame:
    """Price the exact settled WC2026 snapshot with model artifacts fitted through 2025."""
    settings = settings or default_settings()
    catalog = pd.read_csv(settings.path(catalog_path))
    catalog = catalog[
        catalog.match_status.eq("settled") & catalog.outcome.notna()
    ].copy()
    history = pd.read_parquet(settings.path("data/processed/history_stat_table.parquet"))
    elos = _pre_year_elos(history, 2026)
    fold_settings = _settings_from_fold_artifacts(settings, settings.path(fold_artifacts))
    engine = build_engine(fold_settings)
    hosts = set(settings.raw.get("tournament", {}).get("host_teams", []))
    records = []
    for match_id, group in catalog.groupby("match_id", sort=False):
        first = group.iloc[0]
        home, away = catalog_teams(first.match_name)
        kickoff = pd.Timestamp(first.kickoff)
        stage = "knockout" if kickoff >= pd.Timestamp("2026-06-28", tz="UTC") else "group"
        ctx = MatchContext(
            home, away, elo_a=elos.get(home, 1500.0), elo_b=elos.get(away, 1500.0),
            stage=stage, host_a=home in hosts, host_b=away in hosts,
        )
        predictions = engine.predict_many(ctx, group.question.tolist(), n_sims=n_sims)
        for (_, row), prediction in zip(group.iterrows(), predictions):
            probability = float(prediction.p_final)
            outcome = int(row.outcome)
            records.append({
                "match_id": str(match_id), "match_name": str(row.match_name),
                "question": str(row.question), "outcome": outcome,
                "contract_key": contract_key(
                    str(prediction.market), prediction.params or {}, stage=stage,
                ),
                "p_model": probability, "model_brier": (probability - outcome) ** 2,
            })
        engine._sim_cache.clear()
        engine._timeline_cache.clear()
        engine._event_cache.clear()
    rows = pd.DataFrame.from_records(records)
    if len(rows) != len(catalog):
        raise RuntimeError(f"scored {len(rows)} of {len(catalog)} settled catalog questions")
    return rows


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build simulation-report historical evidence")
    parser.add_argument("--oos-rows", default="notebooks/oos_*/exotic_oos_rows.csv")
    parser.add_argument("--empirical-rows", default="notebooks/exotic_empirical_rates.csv")
    parser.add_argument("--wc2026-rows", default="notebooks/wc2026_simulator_oos_rows.csv")
    parser.add_argument("--score-wc2026", action="store_true")
    parser.add_argument("--score-wc2026-only", action="store_true")
    parser.add_argument("--fold-artifacts", default="/tmp/sphybrid_exotic_folds/2026")
    parser.add_argument("--n-sims", type=int, default=1000)
    parser.add_argument("--out", default="data/processed/simulation_evidence.json")
    args = parser.parse_args(argv)
    settings = default_settings()
    wc_path = settings.path(args.wc2026_rows)
    if args.score_wc2026 or args.score_wc2026_only:
        wc = score_settled_catalog(
            settings, fold_artifacts=args.fold_artifacts, n_sims=args.n_sims,
        )
        wc_path.parent.mkdir(parents=True, exist_ok=True)
        wc.to_csv(wc_path, index=False)
        if args.score_wc2026_only:
            print(f"[simulation-evidence] wc2026_questions={len(wc)} -> {wc_path}")
            return 0
    else:
        wc = pd.read_csv(wc_path)
    if any(token in args.oos_rows for token in "*?["):
        oos_paths = sorted(settings.root.glob(args.oos_rows))
        if not oos_paths:
            raise FileNotFoundError(f"no OOS row files match {args.oos_rows!r}")
        oos = pd.concat((pd.read_csv(path) for path in oos_paths), ignore_index=True)
    else:
        oos = pd.read_csv(settings.path(args.oos_rows))
    # The cached folds predate the exact-catalog audit. Keep only observed semantic contracts. The
    # observed regulation late-goal wording equals the cached include-ET price in group matches,
    # where ET is impossible; knockout rows are deliberately excluded.
    late_et = "goal_window:after_second_hydration:et"
    if "stage" in oos and not oos.contract_key.eq("goal_window:after_second_hydration:reg").any():
        late_reg = oos[oos.contract_key.eq(late_et) & oos.stage.ne("knockout")].copy()
        late_reg["contract_key"] = "goal_window:after_second_hydration:reg"
        late_reg["market"] = "goal_after_second_hydration_reg"
        late_reg["market_name"] = "Goal after second hydration break — regulation"
        oos = pd.concat([oos, late_reg], ignore_index=True)
    oos = oos[~oos.contract_key.isin({
        late_et, "penalty_awarded:match", "penalty_or_red:match", "red_card:reg",
    })].copy()
    empirical_path = settings.path(args.empirical_rows)
    empirical = (
        pd.read_parquet(empirical_path) if empirical_path.suffix == ".parquet"
        else pd.read_csv(empirical_path)
    )
    # Fold CSVs are reusable caches. Canonicalize the one inherent half-scope key written by
    # schema 1 so an old prepared fold does not split equivalent first-half evidence.
    for frame in (oos, empirical, wc):
        frame["contract_key"] = frame["contract_key"].replace({
            "substitution_before_halftime:match": "substitution_before_halftime:reg",
        })
    wc_empirical = tournament_empirical_rates(settings, tournament="WORLDCUP2026")
    artifact = build_evidence_artifact(oos, empirical, wc, wc_empirical)
    target = settings.path(args.out)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(artifact, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    print(f"[simulation-evidence] contracts={len(artifact['contracts'])} -> {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
