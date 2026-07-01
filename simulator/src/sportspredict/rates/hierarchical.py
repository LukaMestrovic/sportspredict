"""Fit Layer-1 rate parameters from the StatsBomb per-match stat table.

Produces an *overlay* that overwrites the baseline blocks in ``config/settings.yaml``:
``baseline_rates``, ``half_share_h1``, ``strength_coeffs`` and ``dispersion.nb_vmr`` (plus
red-card / penalty rates). The strength response is a Poisson regression of each team's
per-match count on its standardized Elo differential, **shrunk** toward zero because the
event-level international sample is small (~hundreds of matches); team-specific style is left
to the shared frailties rather than fit per team. ``statsmodels`` is used when available, with
a moment-based fallback otherwise.

The parameterization matches :mod:`sportspredict.rates.baseline`, where a team's rate is
``base * exp(0.5 * coeff * z)`` with ``z = (Elo_team - Elo_opp)/100`` — so a fitted single-team
log-slope ``b`` maps to ``coeff = 2*b``.
"""

from __future__ import annotations

import copy

import numpy as np
import pandas as pd

from ..types import COUNT_STATS, GOALS

_STATS = (GOALS,) + COUNT_STATS
_COEFF_SHRINK = 0.7          # multiplicative shrink on fitted slopes
_COEFF_CLIP = 0.6            # |coeff| ceiling


def _long_team_rows(table: pd.DataFrame, stat: str) -> pd.DataFrame:
    """Stack home/away into per-team rows with h1/total counts and Elo differential z."""
    frames = []
    has_elo = {"home_elo", "away_elo"}.issubset(table.columns)
    for label, opp in (("home", "away"), ("away", "home")):
        h1 = table[f"{label}_{stat}_h1"]
        h2 = table[f"{label}_{stat}_h2"]
        z = (
            (table[f"{label}_elo"] - table[f"{opp}_elo"]) / 100.0
            if has_elo
            else pd.Series(0.0, index=table.index)
        )
        frames.append(pd.DataFrame({"h1": h1, "total": h1 + h2, "z": z}))
    return pd.concat(frames, ignore_index=True)


def _fit_slope(count: np.ndarray, z: np.ndarray) -> float:
    if np.var(z) < 1e-9:
        return 0.0
    try:
        import statsmodels.api as sm

        model = sm.GLM(count, sm.add_constant(z), family=sm.families.Poisson()).fit()
        slope = float(model.params[1])
    except Exception:
        # Moment-based fallback: slope of log(count+0.5) on z.
        y = np.log(count + 0.5)
        slope = float(np.cov(z, y)[0, 1] / np.var(z))
    return float(np.clip(_COEFF_SHRINK * slope, -_COEFF_CLIP / 2, _COEFF_CLIP / 2))


def fit_rates(table: pd.DataFrame) -> dict:
    """Return an overlay dict to merge into settings."""
    baseline_rates: dict[str, float] = {}
    half_share: dict[str, float] = {}
    strength: dict[str, float] = {}
    nb_vmr: dict[str, float] = {}

    for stat in _STATS:
        rows = _long_team_rows(table, stat)
        total = rows["total"].to_numpy(dtype=float)
        baseline_rates[stat] = float(total.mean())
        denom = (rows["h1"] + (rows["total"] - rows["h1"])).sum()
        half_share[stat] = float(rows["h1"].sum() / denom) if denom > 0 else 0.5
        strength[stat] = 2.0 * _fit_slope(total, rows["z"].to_numpy(dtype=float))
        mean = total.mean()
        nb_vmr[stat] = float(np.clip(total.var() / mean if mean > 0 else 1.0, 1.0, 3.0))

    overlay: dict = {
        "baseline_rates": baseline_rates,
        "half_share_h1": half_share,
        "strength_coeffs": strength,
        "dispersion": {"nb_vmr": {k: v for k, v in nb_vmr.items() if k in COUNT_STATS}},
    }
    if "home_reds" in table.columns:
        reds = pd.concat([table["home_reds"], table["away_reds"]]).mean()
        overlay["baseline_rates"]["reds_per_team"] = float(reds)
    if "penalties" in table.columns:
        overlay["baseline_rates"]["penalties_per_match"] = float(table["penalties"].mean())
    return overlay


def apply_overlay(settings_raw: dict, overlay: dict) -> dict:
    """Deep-merge an overlay into a settings dict (returns a new dict)."""
    out = copy.deepcopy(settings_raw)

    def merge(dst: dict, src: dict) -> None:
        for k, v in src.items():
            if isinstance(v, dict) and isinstance(dst.get(k), dict):
                merge(dst[k], v)
            else:
                dst[k] = v

    merge(out, overlay)
    return out
