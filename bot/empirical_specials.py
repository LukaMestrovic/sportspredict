"""Hierarchical calibration for exact event-order/timing specials.

The four reported empirical scopes are nested.  This module first converts
them to disjoint history/current-tournament x group/knockout cells, then fits a
small penalized binomial logit model.  Each settled match therefore contributes
once, while current-tournament and knockout effects are allowed to differ and
the sparsest interaction is shrunk most strongly.
"""
from __future__ import annotations

import math


MODEL_VERSION = "nested-era-stage-logit-v1"
SUPPORTED_CONTRACTS = {
    "goal_window:stoppage:any:reg",
    "first_card_before_first_goal:reg",
}
PRIOR_SIGMAS = (2.5, 0.70, 0.50, 0.35)
TARGET_VECTOR = (1.0, 1.0, 1.0, 1.0)
MAX_ITERATIONS = 60
CONVERGENCE = 1e-10


def calibrate(history: dict, raw_simulator_probability: float | None = None) -> dict | None:
    """Calibrate a WC2026-knockout probability from four nested scopes.

    ``raw_simulator_probability`` is retained as disclosed mechanistic context.
    The calibrated estimate is driven by exact-contract empirical labels until
    rolling-origin validation exists for a match-specific offset on this new
    contract.
    """
    empirical = (history or {}).get("empirical_rate") or {}
    cells = _disjoint_cells(empirical, (history or {}).get("cohort_overlap"))
    if cells is None:
        return None
    beta, covariance = _fit(cells)
    eta = _dot(TARGET_VECTOR, beta)
    variance = _quadratic(TARGET_VECTOR, covariance)
    # Logistic-normal posterior-mean approximation.  It moves uncertain extreme
    # MAP estimates toward 50%, which is appropriate under squared/Brier loss.
    posterior_mean = _sigmoid(eta / math.sqrt(1.0 + math.pi * variance / 8.0))
    sd = math.sqrt(max(variance, 0.0))
    interval = (_sigmoid(eta - 1.6448536269514722 * sd),
                _sigmoid(eta + 1.6448536269514722 * sd))
    return {
        "model_version": MODEL_VERSION,
        "probability": round(posterior_mean, 6),
        "map_probability": round(_sigmoid(eta), 6),
        "posterior_90pct_interval": [round(interval[0], 6), round(interval[1], 6)],
        "target_scope": "wc2026_knockout",
        "cohort_method": (
            "four nested rates converted to disjoint era-by-stage cells; "
            "penalized binomial logit with a strongly shrunk interaction"
        ),
        "priors": {
            "intercept_sigma": PRIOR_SIGMAS[0],
            "wc2026_shift_sigma": PRIOR_SIGMAS[1],
            "knockout_shift_sigma": PRIOR_SIGMAS[2],
            "wc2026_knockout_interaction_sigma": PRIOR_SIGMAS[3],
        },
        "coefficients": {
            name: round(value, 6) for name, value in zip(
                ("intercept", "wc2026_shift", "knockout_shift",
                 "wc2026_knockout_interaction"), beta,
            )
        },
        "disjoint_cells": [
            {
                "scope": cell["scope"], "yes_events": cell["yes_events"],
                "observations": cell["observations"],
                "rate": round(cell["yes_events"] / cell["observations"], 6),
            }
            for cell in cells
        ],
        "raw_simulator_probability": (
            round(float(raw_simulator_probability), 6)
            if _probability(raw_simulator_probability) is not None else None
        ),
        "raw_simulator_role": (
            "exact mechanistic contract check; disclosed but not used as a calibrated "
            "offset until contract-specific rolling-origin validation is available"
        ),
    }


def _disjoint_cells(empirical: dict, overlap: dict | None = None) -> list[dict] | None:
    rows = {name: _counts(empirical.get(name)) for name in (
        "all_history", "all_history_knockout", "wc2026", "wc2026_knockout",
    )}
    if any(value is None for value in rows.values()):
        return None
    hist, hist_ko = rows["all_history"], rows["all_history_knockout"]
    wc, wc_ko = rows["wc2026"], rows["wc2026_knockout"]
    assert hist is not None and hist_ko is not None and wc is not None and wc_ko is not None
    if not (_is_subset(hist_ko, hist) and _is_subset(wc_ko, wc)):
        return None
    included_wc = _counts((overlap or {}).get("wc2026_in_all_history")) or (0, 0)
    if not _is_subset(included_wc, hist):
        return None
    raw = [
        ("historical_non_wc2026_non_knockout",
         hist[0] - hist_ko[0] - included_wc[0],
         hist[1] - hist_ko[1] - included_wc[1],
         (1.0, 0.0, 0.0, 0.0)),
        ("historical_knockout", hist_ko[0], hist_ko[1], (1.0, 0.0, 1.0, 0.0)),
        ("wc2026_non_knockout", wc[0] - wc_ko[0], wc[1] - wc_ko[1],
         (1.0, 1.0, 0.0, 0.0)),
        ("wc2026_knockout", wc_ko[0], wc_ko[1], (1.0, 1.0, 1.0, 1.0)),
    ]
    if any(n <= 0 or y < 0 or y > n for _name, y, n, _x in raw):
        return None
    return [
        {"scope": name, "yes_events": int(y), "observations": int(n), "x": x}
        for name, y, n, x in raw
    ]


def _counts(row: dict | None) -> tuple[int, int] | None:
    if not isinstance(row, dict) or not row.get("available"):
        return None
    try:
        n = int(row.get("observations") or row.get("matches"))
        raw_yes = row.get("yes_events")
        y = int(raw_yes) if raw_yes is not None else int(round(float(row["rate"]) * n))
    except (KeyError, TypeError, ValueError):
        return None
    return (y, n) if n > 0 and 0 <= y <= n else None


def _is_subset(part: tuple[int, int], whole: tuple[int, int]) -> bool:
    return part[0] <= whole[0] and part[1] <= whole[1]


def _fit(cells: list[dict]) -> tuple[list[float], list[list[float]]]:
    total_y = sum(cell["yes_events"] for cell in cells[:2])
    total_n = sum(cell["observations"] for cell in cells[:2])
    beta = [_logit((total_y + 0.5) / (total_n + 1.0)), 0.0, 0.0, 0.0]
    precision = [1.0 / (sigma * sigma) for sigma in PRIOR_SIGMAS]
    for _ in range(MAX_ITERATIONS):
        gradient = [-precision[j] * beta[j] for j in range(4)]
        information = [[precision[i] if i == j else 0.0 for j in range(4)] for i in range(4)]
        for cell in cells:
            x = cell["x"]
            p = _sigmoid(_dot(x, beta))
            residual = cell["yes_events"] - cell["observations"] * p
            weight = cell["observations"] * p * (1.0 - p)
            for i in range(4):
                gradient[i] += x[i] * residual
                for j in range(4):
                    information[i][j] += weight * x[i] * x[j]
        step = _solve(information, gradient)
        beta = [value + delta for value, delta in zip(beta, step)]
        if max(abs(delta) for delta in step) < CONVERGENCE:
            break
    return beta, _inverse(information)


def _solve(matrix: list[list[float]], vector: list[float]) -> list[float]:
    aug = [row[:] + [vector[i]] for i, row in enumerate(matrix)]
    size = len(vector)
    for column in range(size):
        pivot = max(range(column, size), key=lambda row: abs(aug[row][column]))
        if abs(aug[pivot][column]) < 1e-14:
            raise ValueError("singular empirical-special calibration matrix")
        aug[column], aug[pivot] = aug[pivot], aug[column]
        scale = aug[column][column]
        aug[column] = [value / scale for value in aug[column]]
        for row in range(size):
            if row == column:
                continue
            factor = aug[row][column]
            aug[row] = [a - factor * b for a, b in zip(aug[row], aug[column])]
    return [aug[i][-1] for i in range(size)]


def _inverse(matrix: list[list[float]]) -> list[list[float]]:
    return [
        _solve(matrix, [1.0 if row == column else 0.0 for row in range(len(matrix))])
        for column in range(len(matrix))
    ]


def _quadratic(vector: tuple[float, ...], matrix: list[list[float]]) -> float:
    # ``_inverse`` returns inverse columns; symmetry makes the orientation immaterial.
    return sum(vector[i] * matrix[j][i] * vector[j]
               for i in range(len(vector)) for j in range(len(vector)))


def _dot(left, right) -> float:
    return sum(a * b for a, b in zip(left, right))


def _sigmoid(value: float) -> float:
    if value >= 0:
        term = math.exp(-value)
        return 1.0 / (1.0 + term)
    term = math.exp(value)
    return term / (1.0 + term)


def _logit(value: float) -> float:
    bounded = min(max(float(value), 1e-9), 1.0 - 1e-9)
    return math.log(bounded / (1.0 - bounded))


def _probability(value) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and 0.0 <= number <= 1.0 else None
