"""Regulation total-shots model conditional on the simulated shots-on-target total."""

from __future__ import annotations

import numpy as np

from sportspredict.markets.schema import apply_comparator
from sportspredict.types import SHOTS_ON_TARGET, TEAM_A, TEAM_B


def fit_total_shots_model(players, history) -> dict:
    """Fit off-target shots by team from complete player-match rows without extra time."""
    required = {"match_id", "team_side", "shots_total", "shots_on", "reconciles_sot"}
    if players is None or not required.issubset(players.columns):
        return {}
    stage = {
        int(row.match_id): str(row.stage)
        for row in history.itertuples(index=False)
        if str(row.source) == "apifootball"
    }
    valid = players[
        players["match_id"].map(stage).fillna("unknown").ne("knockout")
        & players["reconciles_sot"].fillna(False)
    ]
    teams = (
        valid.groupby(["match_id", "team_side"], as_index=False)
        .agg(shots=("shots_total", "sum"), sot=("shots_on", "sum"))
    )
    if teams.empty:
        return {}
    teams["off"] = np.maximum(teams.shots - teams.sot, 0.0)
    x = teams.sot.to_numpy(float)
    y = teams.off.to_numpy(float)
    design = np.column_stack([np.ones(len(x)), x])
    intercept, slope = np.linalg.lstsq(design, y, rcond=None)[0]
    intercept, slope = max(float(intercept), 0.0), max(float(slope), 0.0)
    baseline = np.maximum(intercept + slope * x, 0.05)
    pearson = float(np.sum((y - baseline) ** 2 / baseline) / max(len(y) - 2, 1))
    vmr = float(np.clip(pearson, 1.0, 4.0))

    lookup = {}
    prior_n = 20.0
    for sot, group in teams.groupby(teams.sot.astype(int)):
        prior = intercept + slope * int(sot)
        mean = (float(group.off.sum()) + prior_n * prior) / (len(group) + prior_n)
        lookup[str(int(sot))] = round(max(mean, 0.0), 6)
    return {
        "n_team_matches": int(len(teams)),
        "intercept": round(intercept, 6),
        "slope": round(slope, 6),
        "vmr": round(vmr, 6),
        "off_target_mean_by_sot": lookup,
    }


def _sample_count(mean: np.ndarray, vmr: float, rng: np.random.Generator) -> np.ndarray:
    mean = np.maximum(np.asarray(mean, dtype=float), 0.0)
    if vmr <= 1.0 + 1e-9:
        return rng.poisson(mean)
    n = np.where(mean > 0, mean / (vmr - 1.0), 1e-9)
    values = rng.negative_binomial(n, 1.0 / vmr)
    return np.where(mean > 0, values, 0)


def total_shots_probability(
    outcome, model: dict | None, comparator: str, threshold: float,
    rng: np.random.Generator,
) -> float:
    """Price match total shots in regulation; never adds extra-time counts."""
    model = model or {}
    intercept = float(model.get("intercept", 6.5))
    slope = float(model.get("slope", 1.1))
    vmr = float(model.get("vmr", 1.5))
    lookup = model.get("off_target_mean_by_sot") or {}
    match_total = np.zeros(outcome.n_sims, dtype=int)
    for team in (TEAM_A, TEAM_B):
        sot = np.asarray(
            outcome.team_total(SHOTS_ON_TARGET, team, include_et=False), dtype=int,
        )
        means = np.asarray([
            float(lookup.get(str(int(value)), intercept + slope * int(value)))
            for value in sot
        ])
        off = _sample_count(means, vmr, rng)
        match_total += sot + off.astype(int)
    return float(np.mean(apply_comparator(match_total, comparator, threshold)))
