"""Rare match events: red cards (per team) and penalties (per match).

Both are driven by the shared physicality frailty, so they co-move with fouls/cards and
with each other — which is what the compound "penalty OR red card" market needs. Yellow
cards are modelled as an ordinary count statistic in :mod:`counts`.
"""

from __future__ import annotations

import numpy as np

from ..rates.params import MatchRates


def sample_reds(
    rng: np.random.Generator,
    rates: MatchRates,
    gamma_tempo: np.ndarray,
    gamma_phys: np.ndarray,
    et_played: np.ndarray,
    et_scale: float,
) -> np.ndarray:
    """Total red cards per team incl. extra time. Shape (2, N)."""
    mask = et_played.astype(float)
    frailty = gamma_tempo * gamma_phys
    reg_mean = rates.reds[:, None] * frailty[None, :]               # (2,N)
    et_mean = reg_mean * et_scale * mask[None, :]
    return (rng.poisson(reg_mean) + rng.poisson(et_mean)).astype(np.int64)


def sample_penalties(
    rng: np.random.Generator,
    rates: MatchRates,
    gamma_tempo: np.ndarray,
    gamma_phys: np.ndarray,
    et_played: np.ndarray,
    et_scale: float,
) -> np.ndarray:
    """Total penalties awarded in the match incl. extra time. Shape (N,)."""
    mask = et_played.astype(float)
    frailty = gamma_tempo * gamma_phys
    reg_mean = rates.penalties * frailty                             # (N,)
    et_mean = reg_mean * et_scale * mask
    return (rng.poisson(reg_mean) + rng.poisson(et_mean)).astype(np.int64)
