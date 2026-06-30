"""Container for vectorized simulation draws.

All arrays have a trailing axis of length ``n_sims``. Counts are split into regulation
(per team, per half) and an extra-time increment (per team). Accessors apply the
"second half = regulation only" and "counts include extra time" rules so resolvers
need not know the storage layout.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from ..types import GOALS, H1, H2, PER_HALF_STATS, TEAM_A, TEAM_B


@dataclass
class MatchOutcome:
    n_sims: int
    # reg_counts[stat]: (2 teams, 2 halves, N) regulation counts.
    reg_counts: dict[str, np.ndarray]
    # et_counts[stat]: (2 teams, N) extra-time increments (0 where no ET played).
    et_counts: dict[str, np.ndarray]
    reds: np.ndarray            # (2, N) total red cards incl. ET
    penalties: np.ndarray       # (N,) total penalties awarded incl. ET
    et_played: np.ndarray       # (N,) bool
    result: np.ndarray          # (N,) in {0:A, 1:draw, 2:B}; knockouts never draw
    gamma_tempo: np.ndarray     # (N,) diagnostics
    gamma_phys: np.ndarray      # (N,)

    # -- per-team statistic accessors --------------------------------------
    def team_half(self, stat: str, team: int, half: int) -> np.ndarray:
        """Regulation count for one team in one half (half 0 = 1st, 1 = 2nd)."""
        return self.reg_counts[stat][team, half, :]

    def team_total(self, stat: str, team: int, include_et: bool = False) -> np.ndarray:
        """Full-match count for one team; optionally add the extra-time increment."""
        total = self.reg_counts[stat][team, :, :].sum(axis=0)
        if include_et:
            total = total + self.et_counts[stat][team, :]
        return total

    def match_total(self, stat: str, include_et: bool = False) -> np.ndarray:
        """Both teams combined."""
        return self.team_total(stat, TEAM_A, include_et) + self.team_total(
            stat, TEAM_B, include_et
        )

    # -- goal helpers (used by half-conditional + result markets) -----------
    def goals_team(self, team: int, include_et: bool = False) -> np.ndarray:
        return self.team_total(GOALS, team, include_et)

    def goals_half(self, team: int, half: int) -> np.ndarray:
        return self.team_half(GOALS, team, half)

    def match_goals_half(self, half: int) -> np.ndarray:
        return self.team_half(GOALS, TEAM_A, half) + self.team_half(GOALS, TEAM_B, half)

    def validate(self) -> None:
        for stat in PER_HALF_STATS:
            assert self.reg_counts[stat].shape == (2, 2, self.n_sims)
            assert self.et_counts[stat].shape == (2, self.n_sims)
