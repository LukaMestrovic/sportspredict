"""Top-level orchestration.

``Engine.predict_many`` simulates a fixture **once** and resolves every question against
those same draws (the per-match caching from the plan), then for vanilla markets de-vigs any
supplied bookmaker odds and shrinks the model probability toward them. Exotic markets keep a
shrink weight of 0, so they are pure model.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from collections.abc import Iterable

import numpy as np

from .config import Settings, default_settings
from .features.context import MatchContext
from .markets import parse_question, resolve
from .markets.devig import devig, devig_outcome
from .markets.schema import MarketSpec, MarketType
from .markets.shrink import shrink_to_market, weight_for_family
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
    p_market: float | None = None
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
        market_odds: dict | None = None,
        n_sims: int | None = None,
    ) -> Prediction:
        return self.predict_many(ctx, [question], market_odds, n_sims)[0]

    def predict_many(
        self,
        ctx: MatchContext,
        questions: list[str],
        market_odds: dict | None = None,
        n_sims: int | None = None,
    ) -> list[Prediction]:
        outcome = self._simulate(ctx, n_sims)
        preds: list[Prediction] = []
        for q in questions:
            spec = parse_question(q, ctx)
            p_model = resolve(spec, outcome, ctx, self.settings)
            p_market = self._market_prob(spec, market_odds)
            w = weight_for_family(spec.family, self.settings)
            p_final = shrink_to_market(p_model, p_market, w)
            preds.append(
                Prediction(
                    question=q,
                    market=spec.market.value,
                    params=spec.params,
                    p_model=p_model,
                    p_market=p_market,
                    p_final=p_final,
                    n_sims=outcome.n_sims,
                    notes=_notes(spec, p_market, w),
                )
            )
        return preds

    # -- internals ----------------------------------------------------------
    def _simulate(self, ctx: MatchContext, n_sims: int | None) -> MatchOutcome:
        n = int(n_sims if n_sims is not None else self.settings.n_sims)
        key = (
            ctx.team_a, ctx.team_b, ctx.elo_a, ctx.elo_b, ctx.stage,
            ctx.host_a, ctx.host_b, ctx.referee_card_mult, ctx.referee_foul_mult,
            ctx.referee_pen_mult, _rate_mult_key(ctx), n,
        )
        if key not in self._sim_cache:
            rates = self.rate_model.build(ctx)
            rng = np.random.default_rng(self.settings.seed)
            self._sim_cache[key] = simulate(rates, n_sims=n, rng=rng, settings=self.settings)
        return self._sim_cache[key]

    def _market_prob(self, spec: MarketSpec, market_odds: dict | None) -> float | None:
        """De-vigged fair probability for the spec's outcome, if odds were supplied.

        Returns ``None`` (=> pure model) whenever the book price does not describe the
        question's exact outcome: half-scoped variants, a totals line that differs from
        the question threshold, or an incomplete outcome set.
        """
        if not market_odds:
            return None
        method = self.settings.markets.get("devig_method", "shin")

        if spec.market == MarketType.MATCH_RESULT and "match_result" in market_odds:
            if not spec.params.get("regulation", True):
                return None  # "advance" (ET/shootout) is not the book's 90' outcome
            book = market_odds["match_result"]  # {"A":o,"draw":o,"B":o}
            if not all(k in book for k in ("A", "draw", "B")):
                return None  # two-way prices cannot be de-vigged as a 1X2
            if spec.params.get("double_chance", False):
                labels = list(book)
                probs = devig([book[k] for k in labels], method=method)
                return float(probs[labels.index(spec.params["side"])] + probs[labels.index("draw")])
            return devig_outcome(book, spec.params["side"], method)

        if spec.market == MarketType.TOTAL_GOALS and "total_goals" in market_odds:
            book = market_odds["total_goals"]  # {"over":o,"under":o,"line":x}
            if spec.params.get("half", "full") != "full":
                return None  # book totals are full-match lines
            if "over" not in book or "under" not in book:
                return None
            line = book.get("line")
            if line is not None and not _threshold_matches_line(
                spec.params["comparator"], float(spec.params["threshold"]), float(line)
            ):
                return None
            under = spec.params["comparator"] in ("<", "<=")
            return devig_outcome({"under": book["under"], "over": book["over"]},
                                 "under" if under else "over", method)

        if spec.market == MarketType.BTTS and "btts" in market_odds:
            if spec.params.get("half", "full") != "full":
                return None  # book BTTS is full-match
            book = market_odds["btts"]  # {"yes":o,"no":o}
            if "yes" not in book or "no" not in book:
                return None
            target = "yes" if spec.params.get("yes", True) else "no"
            return devig_outcome(book, target, method)

        return None


def _threshold_matches_line(comp: str, threshold: float, line: float) -> bool:
    """Does (comparator, threshold) describe the same event as over/under ``line``?

    Only half-integer lines map cleanly (integer lines can push, which a two-way
    de-vig cannot represent): "> 2.5" and ">= 3" are over 2.5; "< 2.5" and "<= 2"
    are under 2.5.
    """
    if line % 1.0 == 0.0:
        return False
    if comp in (">", "<"):
        return threshold == line
    if comp == ">=":
        return threshold - 0.5 == line
    if comp == "<=":
        return threshold + 0.5 == line
    return False


def _rate_mult_key(ctx: MatchContext) -> tuple:
    """Cache key component for odds-anchor multipliers stored on ``ctx.extra``."""
    raw = (ctx.extra or {}).get("rate_mult")
    if not isinstance(raw, dict) or not raw:
        return ()

    def freeze(value):
        if isinstance(value, Iterable) and not isinstance(value, (str, bytes, dict)):
            return tuple(round(float(v), 12) for v in value)
        return round(float(value), 12)

    items = []
    for stat, value in sorted(raw.items()):
        try:
            frozen = freeze(value)
        except (TypeError, ValueError):
            frozen = repr(value)
        items.append((str(stat), frozen))
    return tuple(items)


def _notes(spec: MarketSpec, p_market: float | None, weight: float) -> str:
    if p_market is None:
        return "pure model (no market price)" if weight > 0 else "exotic / model-only"
    return f"shrunk toward market (w={weight:.2f})"
