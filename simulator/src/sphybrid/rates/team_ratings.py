"""Fitted attack/defence team lookup used by training and runtime."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd


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

    def to_frame(self) -> pd.DataFrame:
        teams = sorted(set(self.attack) | set(self.defense))
        return pd.DataFrame({
            "team": teams,
            "attack": [self.attack.get(team, 0.0) for team in teams],
            "defense": [self.defense.get(team, 0.0) for team in teams],
            "n_matches": [self.n_matches.get(team, 0) for team in teams],
        })

    def save(self, path: str | Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.suffix == ".json":
            rows = [
                [
                    str(team),
                    float(self.attack.get(team, 0.0)),
                    float(self.defense.get(team, 0.0)),
                    int(self.n_matches.get(team, 0)),
                ]
                for team in sorted(set(self.attack) | set(self.defense))
            ]
            payload = {
                "schema_version": 1,
                "columns": ["team", "attack", "defense", "n_matches"],
                "intercept": float(self.intercept),
                "home_adv": float(self.home_adv),
                "teams": rows,
            }
            path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            return
        df = self.to_frame()
        df["_intercept"] = self.intercept
        df["_home_adv"] = self.home_adv
        df.to_parquet(path)

    @classmethod
    def load(cls, path: str | Path) -> "TeamRatings":
        path = Path(path)
        if path.suffix == ".json":
            data = json.loads(path.read_text(encoding="utf-8"))
            rows = data.get("teams") or []
            return cls(
                attack={str(team): float(attack) for team, attack, _defense, _n in rows},
                defense={str(team): float(defense) for team, _attack, defense, _n in rows},
                n_matches={str(team): int(n) for team, _attack, _defense, n in rows},
                intercept=float(data.get("intercept", 0.0)),
                home_adv=float(data.get("home_adv", 0.0)),
            )
        df = pd.read_parquet(path)
        intercept = float(df["_intercept"].iloc[0]) if "_intercept" in df and len(df) else 0.0
        home_adv = float(df["_home_adv"].iloc[0]) if "_home_adv" in df and len(df) else 0.0
        return cls(
            attack=dict(zip(df["team"], df["attack"].astype(float))),
            defense=dict(zip(df["team"], df["defense"].astype(float))),
            n_matches=dict(zip(
                df["team"],
                df.get("n_matches", pd.Series(0, index=df.index)).astype(int),
            )),
            intercept=intercept,
            home_adv=home_adv,
        )


def _stack_directed(results: pd.DataFrame) -> pd.DataFrame:
    neutral = (
        results["neutral"].astype(bool).to_numpy()
        if "neutral" in results
        else np.zeros(len(results), bool)
    )

    def side(scoring, conceding, goals, is_home):
        return pd.DataFrame({
            "scoring": results[scoring].astype(str).to_numpy(),
            "conceding": results[conceding].astype(str).to_numpy(),
            "goals": results[goals].astype(float).to_numpy(),
            "is_home": is_home,
        })

    return pd.concat([
        side("home_team", "away_team", "home_score", np.where(neutral, 0.0, 1.0)),
        side("away_team", "home_team", "away_score", np.zeros(len(results))),
    ], ignore_index=True)


def fit_team_ratings(
    results: pd.DataFrame, *, l2: float = 1.0, min_matches: int = 3, max_iter: int = 1000,
) -> TeamRatings:
    try:
        from scipy.sparse import csr_matrix, hstack
        from sklearn.linear_model import PoissonRegressor
        from sklearn.preprocessing import OneHotEncoder
    except Exception as exc:  # pragma: no cover - exercised only when sklearn/scipy is absent
        raise RuntimeError("fit_team_ratings needs scikit-learn and scipy.") from exc

    stacked = _stack_directed(results)
    teams = sorted(set(stacked["scoring"]) | set(stacked["conceding"]))

    enc_att = OneHotEncoder(categories=[teams], handle_unknown="ignore")
    enc_def = OneHotEncoder(categories=[teams], handle_unknown="ignore")
    attack = enc_att.fit_transform(stacked[["scoring"]])
    defense = enc_def.fit_transform(stacked[["conceding"]])
    home = csr_matrix(stacked[["is_home"]].to_numpy(dtype=float))
    design = hstack([attack, defense, home], format="csr")

    model = PoissonRegressor(alpha=l2, fit_intercept=True, max_iter=max_iter)
    model.fit(design, stacked["goals"].to_numpy(dtype=float))

    n = len(teams)
    coef = model.coef_
    attack_coef = coef[:n] - float(np.mean(coef[:n]))
    defense_coef = coef[n:2 * n] - float(np.mean(coef[n:2 * n]))
    counts = stacked["scoring"].value_counts().add(
        stacked["conceding"].value_counts(), fill_value=0,
    ).astype(int)

    attack_map: dict[str, float] = {}
    defense_map: dict[str, float] = {}
    n_matches: dict[str, int] = {}
    for idx, team in enumerate(teams):
        matches = int(counts.get(team, 0))
        n_matches[team] = matches
        if matches >= min_matches:
            attack_map[team] = float(attack_coef[idx])
            defense_map[team] = float(defense_coef[idx])
    return TeamRatings(
        attack=attack_map,
        defense=defense_map,
        n_matches=n_matches,
        intercept=float(model.intercept_),
        home_adv=float(coef[-1]) if len(coef) else 0.0,
    )


def load_team_ratings(path: str | Path | None) -> TeamRatings:
    if path is None or not Path(path).exists():
        return TeamRatings.neutral()
    try:
        return TeamRatings.load(path)
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return TeamRatings.neutral()
