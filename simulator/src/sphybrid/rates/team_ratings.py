"""Runtime loader for the fitted attack/defence team lookup."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class TeamRatings:
    attack: dict[str, float] = field(default_factory=dict)
    defense: dict[str, float] = field(default_factory=dict)
    n_matches: dict[str, int] = field(default_factory=dict)
    intercept: float = 0.0
    home_adv: float = 0.0

    @classmethod
    def neutral(cls) -> "TeamRatings":
        return cls()

    def get(self, team: str) -> tuple[float, float]:
        return self.attack.get(team, 0.0), self.defense.get(team, 0.0)

    @classmethod
    def load(cls, path: str | Path) -> "TeamRatings":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        rows = data.get("teams") or []
        return cls(
            attack={str(team): float(attack) for team, attack, _defense, _n in rows},
            defense={str(team): float(defense) for team, _attack, defense, _n in rows},
            n_matches={str(team): int(n) for team, _attack, _defense, n in rows},
            intercept=float(data.get("intercept", 0.0)),
            home_adv=float(data.get("home_adv", 0.0)),
        )


def load_team_ratings(path: str | Path | None) -> TeamRatings:
    if path is None or not Path(path).exists():
        return TeamRatings.neutral()
    try:
        return TeamRatings.load(path)
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return TeamRatings.neutral()
