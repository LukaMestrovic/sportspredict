from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

import numpy as np


ROOT = Path(__file__).resolve().parents[1]
SIMULATOR = ROOT / "simulator"
if str(SIMULATOR / "src") not in sys.path:
    sys.path.insert(0, str(SIMULATOR / "src"))
os.environ.setdefault("SPORTSPREDICT_ROOT", str(SIMULATOR))

from sportspredict.config import default_settings  # noqa: E402
from sportspredict.engine import Engine  # noqa: E402
from sportspredict.features.context import MatchContext, PlayerInfo  # noqa: E402
from sportspredict.model import cards  # noqa: E402
from sportspredict.model.closed_forms import _dc_joint  # noqa: E402
from sportspredict.model.goals import sample_dixon_coles  # noqa: E402
from sportspredict.model.outcome import MatchOutcome  # noqa: E402
from sportspredict.model.players import prob_score, prob_score_or_assist  # noqa: E402
from sportspredict.rates.params import MatchRates  # noqa: E402
from sportspredict.types import COUNT_STATS, GOALS, H1, PER_HALF_STATS, RESULT_A, TEAM_A  # noqa: E402


class DixonColesAccuracyTests(unittest.TestCase):
    def test_positive_rho_lifts_draw_cells_in_closed_form(self):
        independent = _dc_joint(1.36, 1.36, 0.0, 12)
        draw_lifted = _dc_joint(1.36, 1.36, 0.08, 12)

        self.assertGreater(draw_lifted[0, 0], independent[0, 0])
        self.assertGreater(draw_lifted[1, 1], independent[1, 1])
        self.assertLess(draw_lifted[1, 0], independent[1, 0])
        self.assertLess(draw_lifted[0, 1], independent[0, 1])
        self.assertGreater(np.trace(draw_lifted), np.trace(independent))

    def test_positive_rho_lifts_simulated_draw_rate(self):
        n = 160_000
        mu_a = np.full(n, 1.36)
        mu_b = np.full(n, 1.36)

        rng = np.random.default_rng(19)
        x_ind, y_ind = sample_dixon_coles(rng, mu_a, mu_b, 0.0)
        rng = np.random.default_rng(19)
        x_lift, y_lift = sample_dixon_coles(rng, mu_a, mu_b, 0.08)

        self.assertGreater(
            float(np.mean(x_lift == y_lift)),
            float(np.mean(x_ind == y_ind)) + 0.01,
        )


class SimulationCacheAccuracyTests(unittest.TestCase):
    def test_odds_anchor_multiplier_changes_simulation_cache_key(self):
        settings = default_settings()
        engine = Engine(settings=settings, rate_model=_TinyAnchoredRateModel())
        ctx = MatchContext("A", "B")

        ctx.extra["rate_mult"] = {"goals": 0.5}
        low = engine._simulate(ctx, 4000)

        ctx.extra["rate_mult"] = {"goals": 2.0}
        high = engine._simulate(ctx, 4000)

        self.assertIsNot(low, high)
        self.assertGreater(
            float(high.match_total(GOALS, include_et=False).mean()),
            float(low.match_total(GOALS, include_et=False).mean()) * 2.5,
        )


class PlayerAttributionAccuracyTests(unittest.TestCase):
    def test_score_or_assist_adds_mutually_exclusive_per_goal_shares(self):
        outcome = _one_goal_outcome()
        target = PlayerInfo(
            "Target Forward", "A", "FW", goal_rate=0.4, assist_rate=0.3,
        )
        lineup = [
            target,
            PlayerInfo("Other Forward", "A", "FW", goal_rate=0.6, assist_rate=0.7),
            PlayerInfo("Goalkeeper", "A", "GK", goal_rate=0.0, assist_rate=0.0),
            *[
                PlayerInfo(f"Defender {index}", "A", "DF", goal_rate=0.0, assist_rate=0.0)
                for index in range(4)
            ],
            *[
                PlayerInfo(f"Midfielder {index}", "A", "MF", goal_rate=0.0, assist_rate=0.0)
                for index in range(4)
            ],
        ]

        self.assertAlmostEqual(
            prob_score(outcome, TEAM_A, target, lineup=lineup), 0.4,
        )
        self.assertAlmostEqual(
            prob_score_or_assist(outcome, TEAM_A, target, lineup=lineup),
            0.4 + 0.3 * 0.7,
        )


class RareEventAccuracyTests(unittest.TestCase):
    def test_penalties_scale_with_tempo_and_physicality(self):
        n = 80_000
        rates = _rates_with_rare_events()
        gamma_tempo = np.concatenate([
            np.full(n, 2.0),
            np.full(n, 0.5),
        ])
        gamma_phys = np.ones(2 * n)
        et_played = np.zeros(2 * n, dtype=bool)

        draws = cards.sample_penalties(
            np.random.default_rng(7), rates, gamma_tempo, gamma_phys, et_played, 0.3,
        )

        self.assertGreater(float(draws[:n].mean()), float(draws[n:].mean()) * 3.0)


class _TinyAnchoredRateModel:
    def build(self, ctx: MatchContext) -> MatchRates:
        raw = (ctx.extra or {}).get("rate_mult", {}).get("goals", 1.0)
        mult = float(raw[0] if isinstance(raw, (list, tuple)) else raw)
        lam = {}
        for stat in PER_HALF_STATS:
            value = 0.9 * mult if stat == GOALS else 0.0
            lam[stat] = np.full((2, 2), value / 2.0, dtype=float)
        return MatchRates(
            lam=lam,
            reds=np.zeros(2, dtype=float),
            penalties=0.0,
            nb_vmr={stat: 1.0 for stat in COUNT_STATS},
            tempo_var=0.0,
            physicality_var=0.0,
            dc_rho=0.0,
            et_fatigue=0.9,
            shootout_conversion=0.75,
            is_knockout=False,
            allow_draw=True,
        )


def _one_goal_outcome(n: int = 5) -> MatchOutcome:
    reg_counts = {
        stat: np.zeros((2, 2, n), dtype=np.int64)
        for stat in PER_HALF_STATS
    }
    reg_counts[GOALS][TEAM_A, H1, :] = 1
    et_counts = {
        stat: np.zeros((2, n), dtype=np.int64)
        for stat in PER_HALF_STATS
    }
    return MatchOutcome(
        n_sims=n,
        reg_counts=reg_counts,
        et_counts=et_counts,
        reds=np.zeros((2, n), dtype=np.int64),
        penalties=np.zeros(n, dtype=np.int64),
        et_played=np.zeros(n, dtype=bool),
        result=np.full(n, RESULT_A, dtype=np.int8),
        gamma_tempo=np.ones(n),
        gamma_phys=np.ones(n),
    )


def _rates_with_rare_events() -> MatchRates:
    lam = {
        stat: np.zeros((2, 2), dtype=float)
        for stat in PER_HALF_STATS
    }
    return MatchRates(
        lam=lam,
        reds=np.array([0.15, 0.15], dtype=float),
        penalties=0.25,
        nb_vmr={stat: 1.0 for stat in COUNT_STATS},
        tempo_var=0.0,
        physicality_var=0.0,
        dc_rho=0.0,
        et_fatigue=0.9,
        shootout_conversion=0.75,
        is_knockout=False,
        allow_draw=True,
    )


if __name__ == "__main__":
    unittest.main()
