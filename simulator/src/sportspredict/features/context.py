"""Match context: the inputs a single fixture provides to the model.

A :class:`MatchContext` is deliberately lightweight and mostly optional — the system
must work pre-match when lineups or the referee are unknown, falling back to priors.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class PlayerInfo:
    """A player available for the score-or-assist market.

    ``goal_rate`` / ``assist_rate`` are per-90 club rates (from soccerdata/FBref) used
    as attribution-share weights; when unknown they fall back to position defaults.
    ``start_prob`` is the probability the player features (1.0 confirmed starter,
    lower for rotation/bench), used to weight exposure when the lineup is uncertain.
    """

    name: str
    team: str
    position: str = "MF"  # FW / MF / DF / GK
    goal_rate: float | None = None      # non-penalty goals per 90
    assist_rate: float | None = None    # assists per 90
    penalty_taker: bool = False
    start_prob: float = 1.0
    expected_minutes: float | None = None


@dataclass
class MatchContext:
    """Everything known about a fixture before kickoff.

    Strength enters via Elo (``elo_a``/``elo_b``); 1500 is a neutral default. Hosts get
    a small advantage. Referee multipliers (``referee_*_mult``) default to 1.0 and are
    populated by the referee model when a referee is known.
    """

    team_a: str
    team_b: str
    elo_a: float = 1500.0
    elo_b: float = 1500.0

    stage: str = "group"               # "group" or "knockout"
    neutral: bool = True               # WC venues are largely neutral
    host_a: bool = False               # team A is a host nation playing at home
    host_b: bool = False

    referee: str | None = None
    referee_card_mult: float = 1.0     # multiplies yellow/red propensity
    referee_foul_mult: float = 1.0
    referee_pen_mult: float = 1.0

    date: str | None = None
    altitude_m: float = 0.0            # venue altitude (Mexico City ≈ 2240m)

    lineup_a: list[PlayerInfo] = field(default_factory=list)
    lineup_b: list[PlayerInfo] = field(default_factory=list)

    extra: dict = field(default_factory=dict)

    @property
    def is_knockout(self) -> bool:
        return self.stage.lower() == "knockout"

    @property
    def elo_diff(self) -> float:
        """Positive when team A is stronger."""
        return self.elo_a - self.elo_b

    def lineup_for(self, team_index: int) -> list[PlayerInfo]:
        return self.lineup_a if team_index == 0 else self.lineup_b
