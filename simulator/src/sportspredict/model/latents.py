"""Shared latent gamma frailties.

Two multiplicative factors (mean 1) are drawn once per simulated match and shared
across statistics, which is what makes markets co-move:

* **tempo** scales all event volume (goals, shots, corners, offsides, fouls, cards),
* **physicality** additionally scales fouls, cards, red cards and penalties.

A Gamma(shape=1/v, scale=v) has mean 1 and variance ``v``; larger ``v`` means more
overdispersion and stronger positive correlation among the statistics it touches.
"""

from __future__ import annotations

import numpy as np


def draw_frailty(rng: np.random.Generator, var: float, n: int) -> np.ndarray:
    """Draw ``n`` gamma frailties with mean 1 and variance ``var`` (``var<=0`` => all 1)."""
    if var <= 0.0:
        return np.ones(n, dtype=float)
    shape = 1.0 / var
    return rng.gamma(shape=shape, scale=var, size=n)
