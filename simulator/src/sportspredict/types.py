"""Shared, dependency-free constants used across the rate model and simulator.

Statistics are indexed in fixed arrays for vectorized Monte-Carlo:
team index 0 = team A, 1 = team B; half index 0 = first half, 1 = second half.
"""

from __future__ import annotations

from typing import Final

# Per-team, per-half statistics modelled by the simulator.
GOALS: Final = "goals"
SHOTS_ON_TARGET: Final = "shots_on_target"
CORNERS: Final = "corners"
FOULS: Final = "fouls"
OFFSIDES: Final = "offsides"
YELLOWS: Final = "yellows"

# Overdispersed count statistics (Negative-Binomial marginals). Goals are handled
# separately by the Dixon-Coles + frailty goal model.
COUNT_STATS: Final[tuple[str, ...]] = (
    SHOTS_ON_TARGET,
    CORNERS,
    FOULS,
    OFFSIDES,
    YELLOWS,
)

# Everything carried per-team per-half (goals first).
PER_HALF_STATS: Final[tuple[str, ...]] = (GOALS,) + COUNT_STATS

# Statistics scaled by the shared physicality frailty (rough/cynical matches).
PHYSICAL_STATS: Final[frozenset[str]] = frozenset({FOULS, YELLOWS})

# Array indices.
TEAM_A: Final = 0
TEAM_B: Final = 1
H1: Final = 0
H2: Final = 1

# Match-result codes (group stage allows DRAW; knockouts never do post-shootout).
RESULT_A: Final = 0
RESULT_DRAW: Final = 1
RESULT_B: Final = 2
