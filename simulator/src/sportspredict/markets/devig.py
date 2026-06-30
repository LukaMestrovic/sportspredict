"""Remove the bookmaker overround to recover fair probabilities.

Three standard methods, selectable in config (default Shin):

* **proportional** — divide each implied probability by their sum (assumes a uniform margin);
* **shin** — models a fraction ``z`` of insider money and shrinks favourites more than
  longshots (corrects favourite-longshot bias), solved for ``Sum(p)=1``;
* **power** — raises implied probabilities to a common exponent until they sum to 1.

Each takes decimal odds for a *complete* set of mutually-exclusive outcomes (e.g. the three
1X2 prices, or the two over/under prices).
"""

from __future__ import annotations

import numpy as np
from scipy.optimize import brentq


def _proportional(q: np.ndarray) -> np.ndarray:
    return q / q.sum()


def _shin(q: np.ndarray) -> np.ndarray:
    s = q.sum()
    if s <= 1.0:
        return q / s

    def total(z: float) -> np.ndarray:
        return (np.sqrt(z * z + 4.0 * (1.0 - z) * q * q / s) - z) / (2.0 * (1.0 - z))

    try:
        z = brentq(lambda z: total(z).sum() - 1.0, 1e-12, 0.5)
        p = total(z)
        return p / p.sum()
    except ValueError:
        return _proportional(q)


def _power(q: np.ndarray) -> np.ndarray:
    s = q.sum()
    if s <= 1.0:
        return q / s
    try:
        c = brentq(lambda c: np.sum(q ** c) - 1.0, 1.0, 50.0)
        p = q ** c
        return p / p.sum()
    except ValueError:
        return _proportional(q)


_METHODS = {"proportional": _proportional, "shin": _shin, "power": _power}


def devig(odds, method: str = "shin") -> np.ndarray:
    """Decimal odds for a complete outcome set -> fair probabilities (sum to 1)."""
    q = 1.0 / np.asarray(odds, dtype=float)
    if method not in _METHODS:
        raise ValueError(f"unknown devig method {method!r}")
    return _METHODS[method](q)


def devig_outcome(odds_by_label: dict[str, float], target: str, method: str = "shin") -> float:
    """De-vig a labelled odds set and return the fair probability of ``target``."""
    labels = list(odds_by_label)
    probs = devig([odds_by_label[k] for k in labels], method=method)
    return float(probs[labels.index(target)])
