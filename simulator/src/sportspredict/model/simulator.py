"""Assemble one vectorized match simulation from the component models.

Draw order (shared frailties first so every component sees the same match character):

    tempo, physicality frailties
      -> regulation goals (DC bivariate, half-split)
      -> result / extra time / shootout
      -> regulation counts (NB) and extra-time count increments
      -> red cards, penalties
"""

from __future__ import annotations

import numpy as np

from ..config import Settings, default_settings
from ..rates.params import MatchRates
from ..types import GOALS
from . import cards, counts, goals
from .latents import draw_frailty
from .outcome import MatchOutcome


def simulate(
    rates: MatchRates,
    n_sims: int | None = None,
    rng: np.random.Generator | None = None,
    settings: Settings | None = None,
) -> MatchOutcome:
    settings = settings or default_settings()
    n = int(n_sims if n_sims is not None else settings.n_sims)
    if rng is None:
        rng = np.random.default_rng(settings.seed)

    gt = draw_frailty(rng, rates.tempo_var, n)
    gp = draw_frailty(rng, rates.physicality_var, n)

    reg_goals = goals.sample_regulation_goals(rng, rates, gt)
    result, et_goals, et_played = goals.resolve_extra_time_and_result(
        rng, rates, reg_goals, gt
    )

    et_scale = (30.0 / 90.0) * rates.et_fatigue
    reg_counts = counts.sample_regulation_counts(rng, rates, gt, gp)
    et_counts = counts.sample_et_counts(rng, rates, gt, gp, et_played, et_scale)

    reds = cards.sample_reds(rng, rates, gp, et_played, et_scale)
    penalties = cards.sample_penalties(rng, rates, gp, et_played, et_scale)

    # Goals share the same per-half / extra-time storage as the count stats.
    reg_counts[GOALS] = reg_goals
    et_counts[GOALS] = et_goals

    outcome = MatchOutcome(
        n_sims=n,
        reg_counts=reg_counts,
        et_counts=et_counts,
        reds=reds,
        penalties=penalties,
        et_played=et_played,
        result=result,
        gamma_tempo=gt,
        gamma_phys=gp,
    )
    outcome.validate()
    return outcome
