"""Shrink model probabilities toward the de-vigged market, in logit space.

``p_final = sigmoid( w * logit(p_market) + (1-w) * logit(p_model) )``

The weight ``w`` is per market family (``config/settings.yaml``): high for vanilla markets
where sharp lines exist (1X2, totals, BTTS) and **zero for exotics**, so the model keeps its
full edge exactly where the bookmaker is weakest. ``w=0`` or a missing market price returns
the model probability unchanged.
"""

from __future__ import annotations

import math

from ..config import Settings, default_settings

_EPS = 1e-6


def _logit(p: float) -> float:
    p = min(max(p, _EPS), 1.0 - _EPS)
    return math.log(p / (1.0 - p))


def _sigmoid(x: float) -> float:
    return 1.0 / (1.0 + math.exp(-x))


def weight_for_family(family: str, settings: Settings | None = None) -> float:
    settings = settings or default_settings()
    return float(settings.markets.get("shrink_weight", {}).get(family, 0.0))


def shrink_to_market(
    p_model: float, p_market: float | None, weight: float
) -> float:
    """Blend model and market probabilities. ``p_market=None`` or ``weight<=0`` => model."""
    if p_market is None or weight <= 0.0:
        return p_model
    weight = min(max(weight, 0.0), 1.0)
    return _sigmoid(weight * _logit(p_market) + (1.0 - weight) * _logit(p_model))
