"""Brier score and its Murphy (calibration-refinement) decomposition.

For binary outcomes the Brier score equals, under binning,

    BS = reliability - resolution + uncertainty

* **uncertainty** = ``o*(1-o)`` (irreducible; depends only on the base rate),
* **resolution**  = how much bin outcome rates differ from the base rate (higher is better),
* **reliability** = calibration error: how far bin forecasts sit from bin outcome rates
  (lower is better).

We also report ECE and log-loss. The competition uses a *weighted* Brier, so
:func:`weighted_brier` mirrors that.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

_EPS = 1e-12


def brier_score(probs, outcomes) -> float:
    p = np.asarray(probs, dtype=float)
    o = np.asarray(outcomes, dtype=float)
    return float(np.mean((p - o) ** 2))


def weighted_brier(probs, outcomes, weights) -> float:
    p = np.asarray(probs, dtype=float)
    o = np.asarray(outcomes, dtype=float)
    w = np.asarray(weights, dtype=float)
    return float(np.sum(w * (p - o) ** 2) / np.sum(w))


def log_loss(probs, outcomes) -> float:
    p = np.clip(np.asarray(probs, dtype=float), _EPS, 1 - _EPS)
    o = np.asarray(outcomes, dtype=float)
    return float(-np.mean(o * np.log(p) + (1 - o) * np.log(1 - p)))


@dataclass
class BrierDecomposition:
    brier: float
    reliability: float
    resolution: float
    uncertainty: float
    ece: float
    log_loss: float
    n: int
    base_rate: float
    skill_score: float  # 1 - BS/uncertainty (a.k.a. Brier Skill Score vs climatology)


def brier_decomposition(probs, outcomes, n_bins: int = 10) -> BrierDecomposition:
    p = np.asarray(probs, dtype=float)
    o = np.asarray(outcomes, dtype=float)
    n = p.size
    obar = float(o.mean()) if n else 0.0
    uncertainty = obar * (1.0 - obar)

    edges = np.linspace(0.0, 1.0, n_bins + 1)
    # Bin index for each forecast (last bin closed on the right).
    idx = np.clip(np.digitize(p, edges[1:-1], right=False), 0, n_bins - 1)

    reliability = 0.0
    resolution = 0.0
    ece = 0.0
    for k in range(n_bins):
        mask = idx == k
        nk = int(mask.sum())
        if nk == 0:
            continue
        pbar_k = float(p[mask].mean())
        obar_k = float(o[mask].mean())
        reliability += nk * (pbar_k - obar_k) ** 2
        resolution += nk * (obar_k - obar) ** 2
        ece += nk * abs(pbar_k - obar_k)
    reliability /= n
    resolution /= n
    ece /= n

    bs = brier_score(p, o)
    skill = 1.0 - bs / uncertainty if uncertainty > 0 else 0.0
    return BrierDecomposition(
        brier=bs,
        reliability=reliability,
        resolution=resolution,
        uncertainty=uncertainty,
        ece=ece,
        log_loss=log_loss(p, o),
        n=n,
        base_rate=obar,
        skill_score=skill,
    )
