"""The numeric rate bundle the simulator consumes.

:class:`MatchRates` holds the *pre-frailty* expected counts for one fixture. The
simulator multiplies these by per-sim gamma frailties and draws counts. Arrays are
shaped ``(2 teams, 2 halves)`` with team 0 = A, half 0 = first half.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from ..types import PER_HALF_STATS


@dataclass(frozen=True)
class MatchRates:
    # lam[stat] -> ndarray (2, 2): expected count for (team, half), before frailties.
    lam: dict[str, np.ndarray]
    # Per-team expected regulation red cards, shape (2,).
    reds: np.ndarray
    # Expected penalties awarded in the match (scalar, pre-physicality-frailty).
    penalties: float

    # Negative-Binomial variance-to-mean ratio per count stat.
    nb_vmr: dict[str, float]

    # Shared frailty variances (gamma, mean 1).
    tempo_var: float
    physicality_var: float

    # Goal-model parameters.
    dc_rho: float
    et_fatigue: float
    shootout_conversion: float

    # Match type.
    is_knockout: bool
    allow_draw: bool  # group stage allows a drawn result

    def __post_init__(self) -> None:
        for stat in PER_HALF_STATS:
            if stat not in self.lam:
                raise ValueError(f"MatchRates missing rate for stat '{stat}'")
            if self.lam[stat].shape != (2, 2):
                raise ValueError(f"lam['{stat}'] must be shape (2,2)")

    def total_lambda(self, stat: str) -> np.ndarray:
        """Per-team per-match expected count (sum over halves), shape (2,)."""
        return self.lam[stat].sum(axis=1)
