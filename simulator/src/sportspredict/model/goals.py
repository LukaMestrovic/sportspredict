"""Goal model: Dixon-Coles bivariate Poisson + shared tempo frailty, with extra time
and penalty shootouts for knockout matches.

Generative definition (per simulated match):

1. Full-match goal means are the per-team goal rates scaled by the shared tempo frailty.
2. Full-match goal totals ``(X, Y)`` are drawn from a Dixon-Coles bivariate Poisson — an
   independent Poisson pair reweighted by ``tau`` on the four low-score cells. Positive
   ``rho`` lifts 0-0/1-1 and lowers 1-0/0-1 to calibrate draws. Sampled exactly by
   rejection.
3. Each total is split across halves by a binomial with the half-share probability (the
   exact conditional half allocation of a Poisson total).
4. Knockout matches tied after regulation play 30' of extra time (rates scaled by
   ``(30/90)*et_fatigue``); still level => a shootout decides who progresses.
"""

from __future__ import annotations

import numpy as np

from ..rates.params import MatchRates
from ..types import GOALS, RESULT_A, RESULT_B, RESULT_DRAW, TEAM_A, TEAM_B

_EPS = 1e-9


def _clip_rho(mu_a: np.ndarray, mu_b: np.ndarray, rho: float) -> np.ndarray:
    """Clip the draw-lift parameter per sim so all four tau cells stay non-negative."""
    with np.errstate(divide="ignore", invalid="ignore"):
        lo00 = np.where(mu_a * mu_b > 0.0, -1.0 / (mu_a * mu_b), -np.inf)
        hi01 = np.where(mu_a > 0.0, 1.0 / mu_a, np.inf)
        hi10 = np.where(mu_b > 0.0, 1.0 / mu_b, np.inf)
    lo = np.maximum(lo00, -1.0) + _EPS
    hi = np.minimum(hi01, hi10) - _EPS
    return np.clip(np.full_like(mu_a, rho), lo, hi)


def _tau(
    x: np.ndarray, y: np.ndarray, mu_a: np.ndarray, mu_b: np.ndarray, rho: np.ndarray
) -> np.ndarray:
    """Dixon-Coles low-score correction factor for each (x, y)."""
    tau = np.ones_like(mu_a)
    m00 = (x == 0) & (y == 0)
    m01 = (x == 0) & (y == 1)
    m10 = (x == 1) & (y == 0)
    m11 = (x == 1) & (y == 1)
    tau[m00] = (1.0 + mu_a * mu_b * rho)[m00]
    tau[m01] = (1.0 - mu_a * rho)[m01]
    tau[m10] = (1.0 - mu_b * rho)[m10]
    tau[m11] = (1.0 + rho)[m11]
    return tau


def sample_dixon_coles(
    rng: np.random.Generator, mu_a: np.ndarray, mu_b: np.ndarray, rho: float
) -> tuple[np.ndarray, np.ndarray]:
    """Sample full-match goal totals from a DC bivariate Poisson (exact, by rejection)."""
    x = rng.poisson(mu_a)
    y = rng.poisson(mu_b)
    if rho == 0.0:
        return x, y

    rho_eff = _clip_rho(mu_a, mu_b, rho)
    # Per-sim acceptance ceiling = max tau over the four special cells and the bulk (=1).
    tau00 = 1.0 + mu_a * mu_b * rho_eff
    tau01 = 1.0 - mu_a * rho_eff
    tau10 = 1.0 - mu_b * rho_eff
    tau11 = 1.0 + rho_eff
    ceil = np.maximum.reduce(
        [np.ones_like(mu_a), tau00, tau01, tau10, tau11]
    )

    pending = np.ones(mu_a.shape[0], dtype=bool)
    while pending.any():
        idx = np.nonzero(pending)[0]
        tau = _tau(x[idx], y[idx], mu_a[idx], mu_b[idx], rho_eff[idx])
        u = rng.random(idx.size)
        accept = u < (tau / ceil[idx])
        # Resample the still-rejected proposals.
        reject = idx[~accept]
        if reject.size:
            x[reject] = rng.poisson(mu_a[reject])
            y[reject] = rng.poisson(mu_b[reject])
        pending[idx[accept]] = False
    return x, y


def _binomial_split(
    rng: np.random.Generator, totals: np.ndarray, p_first_half: float
) -> tuple[np.ndarray, np.ndarray]:
    h1 = rng.binomial(totals, p_first_half)
    return h1, totals - h1


def sample_regulation_goals(
    rng: np.random.Generator, rates: MatchRates, gamma_tempo: np.ndarray
) -> np.ndarray:
    """Return regulation goals as an array shaped (2 teams, 2 halves, N)."""
    n = gamma_tempo.shape[0]
    lam = rates.lam[GOALS]  # (2,2)
    total_a = lam[TEAM_A].sum()
    total_b = lam[TEAM_B].sum()
    mu_a = total_a * gamma_tempo
    mu_b = total_b * gamma_tempo
    x, y = sample_dixon_coles(rng, mu_a, mu_b, rates.dc_rho)

    f1a = lam[TEAM_A, 0] / total_a if total_a > 0 else 0.0
    f1b = lam[TEAM_B, 0] / total_b if total_b > 0 else 0.0
    a1, a2 = _binomial_split(rng, x, f1a)
    b1, b2 = _binomial_split(rng, y, f1b)

    out = np.empty((2, 2, n), dtype=np.int64)
    out[TEAM_A, 0], out[TEAM_A, 1] = a1, a2
    out[TEAM_B, 0], out[TEAM_B, 1] = b1, b2
    return out


def resolve_extra_time_and_result(
    rng: np.random.Generator, rates: MatchRates, reg_goals: np.ndarray, gamma_tempo: np.ndarray
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Resolve match result, returning (result_code, et_goals, et_played).

    ``et_goals`` is shaped (2, N) (0 where no ET). ``result_code`` is in {A, draw, B};
    knockouts never return draw (decided by ET goals or shootout).
    """
    n = gamma_tempo.shape[0]
    reg_a = reg_goals[TEAM_A].sum(axis=0)
    reg_b = reg_goals[TEAM_B].sum(axis=0)
    et_goals = np.zeros((2, n), dtype=np.int64)

    if not rates.is_knockout:
        result = np.full(n, RESULT_DRAW, dtype=np.int8)
        result[reg_a > reg_b] = RESULT_A
        result[reg_a < reg_b] = RESULT_B
        return result, et_goals, np.zeros(n, dtype=bool)

    # Knockout: regulation winner advances directly; ties go to extra time.
    result = np.full(n, RESULT_DRAW, dtype=np.int8)
    result[reg_a > reg_b] = RESULT_A
    result[reg_a < reg_b] = RESULT_B
    tied = reg_a == reg_b
    et_played = tied.copy()

    if tied.any():
        scale = (30.0 / 90.0) * rates.et_fatigue
        lam = rates.lam[GOALS]
        idx = np.nonzero(tied)[0]
        mu_a = lam[TEAM_A].sum() * gamma_tempo[idx] * scale
        mu_b = lam[TEAM_B].sum() * gamma_tempo[idx] * scale
        ea = rng.poisson(mu_a)
        eb = rng.poisson(mu_b)
        et_goals[TEAM_A, idx] = ea
        et_goals[TEAM_B, idx] = eb
        result[idx[ea > eb]] = RESULT_A
        result[idx[ea < eb]] = RESULT_B

        # Still level after ET -> shootout.
        still = idx[ea == eb]
        if still.size:
            winner_a = _shootout(rng, still.size, rates.shootout_conversion)
            res = np.where(winner_a, RESULT_A, RESULT_B).astype(np.int8)
            result[still] = res

    return result, et_goals, et_played


def _shootout(rng: np.random.Generator, n: int, p: float) -> np.ndarray:
    """Simulate best-of-5 + sudden-death shootouts. Returns True where team A wins."""
    # First five kicks each (approximation: winner distribution is what the market needs).
    a = rng.binomial(5, p, size=n)
    b = rng.binomial(5, p, size=n)
    out = np.empty(n, dtype=bool)
    decided = a != b
    out[decided] = a[decided] > b[decided]

    # Sudden death for the rest: paired kicks until exactly one scores.
    rem = np.nonzero(~decided)[0]
    while rem.size:
        ka = rng.random(rem.size) < p
        kb = rng.random(rem.size) < p
        a_wins = ka & ~kb
        b_wins = kb & ~ka
        out[rem[a_wins]] = True
        out[rem[b_wins]] = False
        rem = rem[~(a_wins | b_wins)]
    return out
