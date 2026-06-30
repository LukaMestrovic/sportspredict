"""Baseline Layer-1 rate model.

Turns a :class:`MatchContext` into :class:`MatchRates` using the documented priors in
``config/settings.yaml``. This is the runnable default; once event data is ingested,
``rates.hierarchical`` overwrites the coefficient blocks via partial-pooling fits and
this same class consumes the fitted ``Settings``.

Construction (per statistic):

    z      = (elo_a - elo_b)/100  + host adjustment           # standardized strength
    s_a    = +0.5 * coeff[stat] * z ;  s_b = -0.5 * coeff[stat] * z
    lam_t  = base[stat] * exp(s_t)                            # per-team per-match
    lam_th = lam_t * half_share(stat, h)                      # split across halves

Cards/fouls additionally get knockout and tight-game multipliers and the referee
multipliers carried on the context. Goals, shots, corners and offsides shift toward the
stronger side; fouls/cards shift slightly toward the underdog.
"""

from __future__ import annotations

import math

import numpy as np

from ..config import Settings, default_settings
from ..features.context import MatchContext
from ..types import (
    CORNERS,
    COUNT_STATS,
    FOULS,
    GOALS,
    OFFSIDES,
    PER_HALF_STATS,
    SHOTS_ON_TARGET,
    YELLOWS,
)
from .params import MatchRates


class RateModel:
    def __init__(self, settings: Settings | None = None) -> None:
        self.s = settings or default_settings()

    # -- strength -----------------------------------------------------------
    def _standardized_strength(self, ctx: MatchContext) -> float:
        """Positive => team A stronger. Includes a host-advantage shift."""
        z = ctx.elo_diff / 100.0
        bonus = float(self.s.context_effects.get("host_supremacy_bonus", 0.0))
        if ctx.host_a:
            z += bonus
        if ctx.host_b:
            z -= bonus
        return z

    # -- main ---------------------------------------------------------------
    def build(self, ctx: MatchContext) -> MatchRates:
        base = self.s.baseline_rates
        coeffs = self.s.strength_coeffs
        h1share = self.s.half_share_h1
        ctx_eff = self.s.context_effects
        z = self._standardized_strength(ctx)

        lam: dict[str, np.ndarray] = {}
        for stat in PER_HALF_STATS:
            coeff = float(coeffs.get(stat, 0.0))
            s_team = np.array([+0.5 * coeff * z, -0.5 * coeff * z])  # (2,)
            per_match = float(base[stat]) * np.exp(s_team)           # (2,)

            # Context multipliers (applied per stat).
            per_match = per_match * self._stat_multiplier(stat, ctx, z)

            # Split across halves.
            f1 = float(h1share[stat])
            arr = np.empty((2, 2), dtype=float)
            arr[:, 0] = per_match * f1
            arr[:, 1] = per_match * (1.0 - f1)
            lam[stat] = arr

        reds = self._red_rates(ctx)
        penalties = self._penalty_rate(ctx)

        disp = self.s.dispersion
        gm = self.s.goals_model
        return MatchRates(
            lam=lam,
            reds=reds,
            penalties=penalties,
            nb_vmr={k: float(v) for k, v in disp["nb_vmr"].items()},
            tempo_var=float(disp["tempo_frailty_var"]),
            physicality_var=float(disp["physicality_frailty_var"]),
            dc_rho=float(gm["dixon_coles_rho"]),
            et_fatigue=float(gm["et_fatigue"]),
            shootout_conversion=float(gm["shootout_conversion"]),
            is_knockout=ctx.is_knockout,
            allow_draw=not ctx.is_knockout,
        )

    # -- per-stat context multipliers --------------------------------------
    def _stat_multiplier(self, stat: str, ctx: MatchContext, z: float) -> float:
        ce = self.s.context_effects
        mult = 1.0
        if stat == YELLOWS:
            mult *= ctx.referee_card_mult
            if ctx.is_knockout:
                mult *= float(ce.get("knockout_yellow_mult", 1.0))
            mult *= self._tight_game_mult(z)
        elif stat == FOULS:
            mult *= ctx.referee_foul_mult
            if ctx.is_knockout:
                mult *= float(ce.get("knockout_foul_mult", 1.0))
        return mult

    def _tight_game_mult(self, z: float) -> float:
        """Closely-matched games are tenser -> mild card inflation; fades by |z|>=2."""
        peak = float(self.s.context_effects.get("tight_game_yellow_mult_max", 1.0))
        closeness = max(0.0, 1.0 - abs(z) / 2.0)
        return 1.0 + (peak - 1.0) * closeness

    def _red_rates(self, ctx: MatchContext) -> np.ndarray:
        base = float(self.s.baseline_rates["reds_per_team"])
        mult = ctx.referee_card_mult
        if ctx.is_knockout:
            mult *= float(self.s.context_effects.get("knockout_red_mult", 1.0))
        return np.array([base * mult, base * mult], dtype=float)

    def _penalty_rate(self, ctx: MatchContext) -> float:
        return float(self.s.baseline_rates["penalties_per_match"]) * ctx.referee_pen_mult
