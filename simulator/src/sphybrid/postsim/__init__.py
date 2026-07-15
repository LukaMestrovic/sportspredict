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
    CARDS_MORE_THAN_GOALS,
    COMPOUND_AND,
    EXACT_GOAL_MARGIN,
    FIRST_GOAL,
    FIRST_GOAL_HALF,
    FIRST_CARD_BEFORE_FIRST_GOAL,
    FIRST_HYDRATION_MINUTE,
    GOAL_WINDOW,
    LEAD_ANY_TIME,
    PLAYER_FULL_MATCH,
    RED_CARD,
    REGULATION_STANDARD,
    SECOND_HYDRATION_MINUTE,
    STAT_WINDOW,
    SUBSTITUTE_GOAL_INVOLVEMENT,
    SUBSTITUTE_SCORE,
    TEAM_SCORE_NO_OWN,
    TEAM_CORNERS_AND_TOTAL_SHOTS_MORE,
    SUBSTITUTION_BEFORE_HALF,
    TOTAL_SHOTS_THRESHOLD,
    WIN_BOTH_HALVES,
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
    "FIRST_GOAL_HALF",
    "FIRST_CARD_BEFORE_FIRST_GOAL",
    "GOAL_WINDOW",
    "COMPOUND_AND",
    "CARD_WINDOW",
    "STAT_WINDOW",
    "SUBSTITUTION_BEFORE_HALF",
    "SUBSTITUTE_SCORE",
    "SUBSTITUTE_GOAL_INVOLVEMENT",
    "TEAM_SCORE_NO_OWN",
    "TEAM_CORNERS_AND_TOTAL_SHOTS_MORE",
    "ANY_PLAYER_THRESHOLD",
    "REGULATION_STANDARD",
    "RED_CARD",
    "BOTH_TEAMS_CARD",
    "LEAD_ANY_TIME",
    "CARDS_MORE_THAN_GOALS",
    "PLAYER_FULL_MATCH",
    "TOTAL_SHOTS_THRESHOLD",
    "WIN_MARGIN",
    "WIN_BOTH_HALVES",
    "EXACT_GOAL_MARGIN",
    "FIRST_HYDRATION_MINUTE",
    "SECOND_HYDRATION_MINUTE",
]
