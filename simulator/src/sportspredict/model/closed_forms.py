"""Closed-form market probabilities (Layer 3).

Exact (up to 1-D numerical integration over the tempo frailty) probabilities for the
markets that are "free" in closed form:

* **1X2** and **total goals** from the Dixon-Coles bivariate Poisson goal model;
* **count thresholds for non-physical stats** (e.g. offsides) from the Negative-Binomial
  marginal — using the fact that NBs with a common ``p`` are closed under addition, so the
  match total given the frailty is itself Negative-Binomial.

These are used (a) directly / as control variates to cut Monte-Carlo variance on vanilla
markets, and (b) in tests to validate the simulator (the MC frequency must match within
sampling error). They cover **regulation** outcomes (no extra time); the simulator handles
extra-time-inclusive knockout resolution.
"""

from __future__ import annotations

import numpy as np
from scipy import stats

from ..rates.params import MatchRates
from ..types import GOALS, PHYSICAL_STATS, TEAM_A, TEAM_B

_EPS = 1e-9


def frailty_nodes(var: float, m: int = 64) -> np.ndarray:
    """Equal-weight quadrature nodes for a mean-1, variance-``var`` gamma frailty."""
    if var <= 0.0:
        return np.ones(1)
    a = 1.0 / var
    probs = (np.arange(m) + 0.5) / m
    return stats.gamma.ppf(probs, a=a, scale=var)


def _clip_rho_scalar(mu_a: float, mu_b: float, rho: float) -> float:
    lo00 = -1.0 / (mu_a * mu_b) if mu_a > 0.0 and mu_b > 0.0 else float("-inf")
    hi01 = 1.0 / mu_a if mu_a > 0.0 else float("inf")
    hi10 = 1.0 / mu_b if mu_b > 0.0 else float("inf")
    lo = max(lo00, -1.0) + _EPS
    hi = min(hi01, hi10) - _EPS
    return float(np.clip(rho, lo, hi))


def _dc_joint(mu_a: float, mu_b: float, rho: float, k: int) -> np.ndarray:
    """Dixon-Coles joint pmf over goals (rows=A 0..k, cols=B 0..k)."""
    x = np.arange(k + 1)
    joint = np.outer(stats.poisson.pmf(x, mu_a), stats.poisson.pmf(x, mu_b))
    if rho != 0.0:
        r = _clip_rho_scalar(mu_a, mu_b, rho)
        joint[0, 0] *= 1.0 + mu_a * mu_b * r
        joint[0, 1] *= 1.0 - mu_a * r
        joint[1, 0] *= 1.0 - mu_b * r
        joint[1, 1] *= 1.0 + r
    return joint / joint.sum()


def prob_1x2(rates: MatchRates, m: int = 64, k: int = 15) -> tuple[float, float, float]:
    """Regulation (P(A win), P(draw), P(B win)), integrated over the tempo frailty."""
    ta = float(rates.total_lambda(GOALS)[TEAM_A])
    tb = float(rates.total_lambda(GOALS)[TEAM_B])
    nodes = frailty_nodes(rates.tempo_var, m)
    pa = pd = pb = 0.0
    for g in nodes:
        joint = _dc_joint(ta * g, tb * g, rates.dc_rho, k)
        pa += np.tril(joint, -1).sum()   # X > Y
        pd += np.trace(joint)            # X == Y
        pb += np.triu(joint, 1).sum()    # X < Y
    n = len(nodes)
    return pa / n, pd / n, pb / n


def prob_total_goals_leq(rates: MatchRates, t: int, m: int = 64, k: int = 15) -> float:
    """Regulation P(total goals <= t), integrated over the tempo frailty."""
    ta = float(rates.total_lambda(GOALS)[TEAM_A])
    tb = float(rates.total_lambda(GOALS)[TEAM_B])
    xs = np.arange(k + 1)
    totals = xs[:, None] + xs[None, :]
    mask = totals <= t
    nodes = frailty_nodes(rates.tempo_var, m)
    acc = 0.0
    for g in nodes:
        acc += _dc_joint(ta * g, tb * g, rates.dc_rho, k)[mask].sum()
    return acc / len(nodes)


def prob_count_total_geq(rates: MatchRates, stat: str, threshold: int, m: int = 64) -> float:
    """Regulation P(match total of a non-physical count stat >= threshold).

    Given the tempo frailty g, the match total is Negative-Binomial because the four
    per-(team, half) NB components share p = 1/vmr and are closed under addition.
    """
    if stat in PHYSICAL_STATS:
        raise ValueError(
            f"closed form unavailable for physical stat '{stat}' (two shared frailties); use MC"
        )
    vmr = rates.nb_vmr.get(stat, 1.0)
    s = float(rates.lam[stat].sum())  # total rate over both teams + halves
    nodes = frailty_nodes(rates.tempo_var, m)
    acc = 0.0
    for g in nodes:
        mean = s * g
        if vmr <= 1.0 + 1e-9:
            cdf = stats.poisson.cdf(threshold - 1, mean)
        else:
            n = mean / (vmr - 1.0)
            p = 1.0 / vmr
            cdf = stats.nbinom.cdf(threshold - 1, n, p)
        acc += 1.0 - cdf
    return acc / len(nodes)
