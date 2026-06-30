"""Canonical market specification.

A :class:`MarketSpec` is the deterministic, machine-readable form of a question. The NL
parser produces it and the resolvers consume it — the simulator never sees question text.
``market.value`` doubles as the *family* key used for de-vig/shrink configuration.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class MarketType(str, Enum):
    MATCH_RESULT = "match_result"                       # 1X2 (vanilla)
    TOTAL_GOALS = "total_goals"                         # over/under total goals (vanilla)
    BTTS = "btts"                                       # both teams to score (vanilla)
    TEAM_VS_TEAM_MORE = "team_vs_team_more"             # P(A_stat > B_stat)
    COUNT_THRESHOLD = "count_threshold"                 # P(count >|>=|<|<= threshold)
    HALF_CONDITIONAL = "half_conditional"               # goals by half
    PLAYER_SCORE_OR_ASSIST = "player_score_or_assist"
    PLAYER_SCORE = "player_score"                       # player to score a goal
    PLAYER_STAT = "player_stat"                         # player count prop (e.g. shots on target)
    PENALTY_OR_RED = "penalty_or_red"                   # compound: penalty OR red card
    PENALTY_AWARDED = "penalty_awarded"                 # a penalty kick is awarded
    BTTS_AND_TOTAL = "btts_and_total"                   # BTTS AND match total threshold
    WIN_TO_NIL = "win_to_nil"                           # team wins (90') without conceding
    CLEAN_SHEET = "clean_sheet"                         # team concedes no goals
    GOES_TO_ET = "goes_to_extra_time"                   # knockout tied after 90'
    GOES_TO_SHOOTOUT = "goes_to_shootout"               # still tied after extra time
    TOTAL_GOALS_PARITY = "total_goals_parity"           # odd/even total goals


# Comparators stored as plain strings; ``apply_comparator`` evaluates them.
COMPARATORS = {">", ">=", "<", "<=", "=="}


def apply_comparator(values, comparator: str, threshold: float):
    if comparator == ">":
        return values > threshold
    if comparator == ">=":
        return values >= threshold
    if comparator == "<":
        return values < threshold
    if comparator == "<=":
        return values <= threshold
    if comparator == "==":
        return values == threshold
    raise ValueError(f"unknown comparator {comparator!r}")


@dataclass
class MarketSpec:
    """A parsed market.

    ``params`` holds market-specific fields, documented per type in :mod:`resolvers`:

    * MATCH_RESULT: ``side`` in {"A","B","draw"}; ``double_chance`` (bool) = side wins or draws
    * TOTAL_GOALS: ``comparator``, ``threshold`` (integer goal count)
    * BTTS: ``yes`` (bool), ``half`` in {"full","1H","2H"}
    * WIN_TO_NIL / CLEAN_SHEET: ``team`` in {"A","B"}
    * TOTAL_GOALS_PARITY: ``parity`` in {"odd","even"}, ``half``
    * TEAM_VS_TEAM_MORE: ``stat``, ``subject`` in {"A","B"}, ``half`` in {"full","2H","1H"}
    * COUNT_THRESHOLD: ``stat``, ``scope`` in {"match","team"}, ``team`` (or None),
      ``comparator``, ``threshold``, ``half`` in {"full","1H","2H"}
    * HALF_CONDITIONAL: ``subtype``, optional ``team``, optional ``half``
    * PLAYER_SCORE_OR_ASSIST: ``player`` (name), ``team`` in {"A","B"} or None
    * PENALTY_OR_RED: (no params)
    """

    market: MarketType
    params: dict = field(default_factory=dict)
    raw_question: str = ""

    @property
    def family(self) -> str:
        return self.market.value

    def __str__(self) -> str:
        return f"{self.market.value}({self.params})"
