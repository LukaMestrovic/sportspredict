"""Overdispersed count statistics (shots on target, corners, fouls, offsides, yellows).

Each is Negative-Binomial per team per half. The mean is the Layer-1 rate scaled by the
shared tempo frailty (and the physicality frailty for fouls/cards), giving cross-market
correlation; the per-stat variance-to-mean ratio adds idiosyncratic overdispersion on top.

A Negative-Binomial with mean ``m`` and variance-to-mean ratio ``v`` (>1) is
``NB(n = m/(v-1), p = 1/v)``; ``v <= 1`` falls back to Poisson.
"""

from __future__ import annotations

import numpy as np

from ..rates.params import MatchRates
from ..types import COUNT_STATS, PHYSICAL_STATS, TEAM_A, TEAM_B


def _nb_or_poisson(
    rng: np.random.Generator, mean: np.ndarray, vmr: float
) -> np.ndarray:
    """Draw counts with the given mean and variance-to-mean ratio."""
    mean = np.maximum(mean, 0.0)
    if vmr <= 1.0 + 1e-9:
        return rng.poisson(mean)
    n = mean / (vmr - 1.0)
    p = 1.0 / vmr
    # negative_binomial needs n>0; guard the (rare) zero-mean cells.
    n = np.where(n > 0, n, 1e-9)
    draws = rng.negative_binomial(n, p)
    return np.where(mean > 0, draws, 0)


def _stat_means(rates: MatchRates, stat: str, gt: np.ndarray, gp: np.ndarray) -> np.ndarray:
    """Per-(team, half, sim) NB means: rate * tempo * (physicality if applicable)."""
    lam = rates.lam[stat]  # (2,2)
    frailty = gt * gp if stat in PHYSICAL_STATS else gt  # (N,)
    # (2,2,1) * (N,) -> (2,2,N)
    return lam[:, :, None] * frailty[None, None, :]


def sample_regulation_counts(
    rng: np.random.Generator,
    rates: MatchRates,
    gamma_tempo: np.ndarray,
    gamma_phys: np.ndarray,
) -> dict[str, np.ndarray]:
    out: dict[str, np.ndarray] = {}
    for stat in COUNT_STATS:
        means = _stat_means(rates, stat, gamma_tempo, gamma_phys)  # (2,2,N)
        vmr = rates.nb_vmr.get(stat, 1.0)
        out[stat] = _nb_or_poisson(rng, means, vmr).astype(np.int64)
    return out


def sample_et_counts(
    rng: np.random.Generator,
    rates: MatchRates,
    gamma_tempo: np.ndarray,
    gamma_phys: np.ndarray,
    et_played: np.ndarray,
    et_scale: float,
) -> dict[str, np.ndarray]:
    """Extra-time count increments per team (Poisson; 0 where no ET). Shape (2, N)."""
    out: dict[str, np.ndarray] = {}
    mask = et_played.astype(float)
    for stat in COUNT_STATS:
        total = rates.total_lambda(stat)  # (2,) per-team per-match rate
        frailty = gamma_tempo * gamma_phys if stat in PHYSICAL_STATS else gamma_tempo
        mean = total[:, None] * frailty[None, :] * et_scale * mask[None, :]  # (2,N)
        out[stat] = rng.poisson(mean).astype(np.int64)
    return out
