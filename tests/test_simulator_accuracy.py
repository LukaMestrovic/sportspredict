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
from sportspredict.features.context import MatchContext  # noqa: E402
from sportspredict.model.closed_forms import _dc_joint  # noqa: E402
from sportspredict.model.goals import sample_dixon_coles  # noqa: E402
from sportspredict.rates.params import MatchRates  # noqa: E402
from sportspredict.types import COUNT_STATS, GOALS, PER_HALF_STATS  # noqa: E402


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


if __name__ == "__main__":
    unittest.main()
