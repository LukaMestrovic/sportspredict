"""Top-level orchestration.

``Engine.predict_many`` simulates a fixture once and resolves every question
against those same draws. The simulator is model-only.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from .config import Settings, default_settings
from .features.context import MatchContext
from .markets import parse_question, resolve
from .model import simulate
from .model.outcome import MatchOutcome
from .rates import RateModel


@dataclass
class Prediction:
    question: str
    market: str
    params: dict
    p_model: float
    p_final: float
    n_sims: int = 0
    notes: str = ""


@dataclass
class Engine:
    settings: Settings = field(default_factory=default_settings)
    rate_model: RateModel | None = None
    _sim_cache: dict = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        if self.rate_model is None:
            self.rate_model = RateModel(self.settings)

    # -- public API ---------------------------------------------------------
    def predict(
        self,
        ctx: MatchContext,
        question: str,
        n_sims: int | None = None,
    ) -> Prediction:
        return self.predict_many(ctx, [question], n_sims)[0]

    def predict_many(
        self,
        ctx: MatchContext,
        questions: list[str],
        n_sims: int | None = None,
    ) -> list[Prediction]:
        outcome = self._simulate(ctx, n_sims)
        preds: list[Prediction] = []
        for q in questions:
            spec = parse_question(q, ctx)
            p_model = resolve(spec, outcome, ctx, self.settings)
            preds.append(
                Prediction(
                    question=q,
                    market=spec.market.value,
                    params=spec.params,
                    p_model=p_model,
                    p_final=p_model,
                    n_sims=outcome.n_sims,
                    notes="model-only",
                )
            )
        return preds

    # -- internals ----------------------------------------------------------
    def _simulate(self, ctx: MatchContext, n_sims: int | None) -> MatchOutcome:
        n = int(n_sims if n_sims is not None else self.settings.n_sims)
        key = (
            ctx.team_a, ctx.team_b, ctx.elo_a, ctx.elo_b, ctx.stage,
            ctx.host_a, ctx.host_b, ctx.referee_card_mult, ctx.referee_foul_mult,
            ctx.referee_pen_mult, n,
        )
        if key not in self._sim_cache:
            rates = self.rate_model.build(ctx)
            rng = np.random.default_rng(self.settings.seed)
            self._sim_cache[key] = simulate(rates, n_sims=n, rng=rng, settings=self.settings)
        return self._sim_cache[key]
