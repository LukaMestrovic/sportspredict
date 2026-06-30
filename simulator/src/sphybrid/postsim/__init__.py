"""Post-simulation layer: answers question types the frozen simulator cannot.

* ``timeline`` — places simulated goals/cards/corners/offsides at historically learned event times.
* ``markets`` — the conservative grammar for timing, player-aggregate and compound questions.
* ``allocation`` — player count props as a validated share of the simulated team total.

All of it sits *after* simulation and never touches the learned rates or the baseline; questions the
baseline already handles are left untouched (see ``markets.parse_extended``).
"""

from __future__ import annotations

from .markets import (
    ANY_PLAYER_THRESHOLD,
    BOTH_TEAMS_CARD,
    CARD_WINDOW,
    COMPOUND_AND,
    FIRST_GOAL,
    FIRST_HYDRATION_MINUTE,
    GOAL_WINDOW,
    RED_CARD,
    REGULATION_STANDARD,
    SECOND_HYDRATION_MINUTE,
    STAT_WINDOW,
    SUBSTITUTE_SCORE,
    TEAM_SCORE_NO_OWN,
    SUBSTITUTION_BEFORE_HALF,
    TOTAL_SHOTS_THRESHOLD,
    WIN_MARGIN,
    ExtSpec,
    parse_extended,
    resolve_extended,
)
from .timeline import GoalTimeline

__all__ = [
    "GoalTimeline",
    "ExtSpec",
    "parse_extended",
    "resolve_extended",
    "FIRST_GOAL",
    "GOAL_WINDOW",
    "COMPOUND_AND",
    "CARD_WINDOW",
    "STAT_WINDOW",
    "SUBSTITUTION_BEFORE_HALF",
    "SUBSTITUTE_SCORE",
    "TEAM_SCORE_NO_OWN",
    "ANY_PLAYER_THRESHOLD",
    "REGULATION_STANDARD",
    "RED_CARD",
    "BOTH_TEAMS_CARD",
    "TOTAL_SHOTS_THRESHOLD",
    "WIN_MARGIN",
    "FIRST_HYDRATION_MINUTE",
    "SECOND_HYDRATION_MINUTE",
]
