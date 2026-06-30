from __future__ import annotations

import copy
from dataclasses import dataclass, field

import numpy as np

from sportspredict.config import Settings, default_settings
from sportspredict.engine import Engine, Prediction

from .adjust import MatchAdjustments, adjustments_from_market
from .postsim import GoalTimeline, parse_extended, resolve_extended
from .postsim import REGULATION_STANDARD
from .rates import make_rate_model

@dataclass
class SimulatorEngine(Engine):
    """Baseline simulator plus learned rates and post-simulation contracts.

    1. **Odds anchor** (live): rescale the goals/corners/cards lambdas to sharp bookmaker-implied
       means (via ``ctx.extra['rate_mult']``) before simulation.
    2. **Post-simulation layer**: answer event timing/player-allocation families and exact-scope
       contracts the baseline resolver cannot represent correctly. It adds timestamped events and
       wraps only explicit regulation/card/player wording; unrelated contracts still use the baseline.
    """

    adjust_log: list = field(default_factory=list, repr=False)
    _timeline_cache: dict = field(default_factory=dict, repr=False)
    _event_cache: dict = field(default_factory=dict, repr=False)

    def predict_many(self, ctx, questions, market_odds=None, n_sims=None) -> list[Prediction]:
        # One timeline belongs to one match simulation. Do not retain its event arrays across the
        # reports (or risk both memory growth and a recycled ``id`` collision).
        self._timeline_cache.clear()
        self._event_cache.clear()
        adj = self._adjustments(ctx, market_odds)
        ctx_eff = ctx
        if not adj.is_empty():
            ctx_eff = copy.deepcopy(ctx)  # never mutate the caller's context; the sim caches on its Elo
            adj.apply_to_context(ctx_eff)
        return self._predict_routed(ctx_eff, list(questions), market_odds, n_sims)

    # -- routing: post-sim extended markets vs the baseline ---------------------------------------
    def _predict_routed(self, ctx, questions, market_odds, n_sims) -> list[Prediction]:
        cfg = self.settings.raw.get("postsim", {})
        if not cfg.get("enabled", True):
            return Engine.predict_many(self, ctx, questions, market_odds, n_sims)

        results: list[Prediction | None] = [None] * len(questions)
        std_idx, std_qs = [], []
        for i, q in enumerate(questions):
            pred = self._try_extended(q, ctx, n_sims, cfg) or self._try_player_alloc(q, ctx, n_sims, cfg)
            if pred is None:
                std_idx.append(i)
                std_qs.append(q)
            else:
                results[i] = pred
        if std_qs:
            std = Engine.predict_many(self, ctx, std_qs, market_odds, n_sims)
            for j, i in enumerate(std_idx):
                results[i] = std[j]
        return results  # type: ignore[return-value]

    def _try_extended(self, q, ctx, n_sims, cfg) -> Prediction | None:
        """Resolve ``q`` via the post-sim layer, or ``None`` to defer to the baseline."""
        try:
            spec = parse_extended(q, ctx)
        except Exception:  # a parser hiccup must never break a prediction
            spec = None
        if spec is None:
            return None
        try:
            outcome = self._simulate(ctx, n_sims)
            timing = self._timing_model(cfg)
            timeline = self._timeline(outcome, cfg, timing)
            seed = int(self.settings.seed) if self.settings.seed is not None else 0
            event_rng = np.random.default_rng(np.random.SeedSequence([seed, 0xE7_E7]))
            p = resolve_extended(
                spec, timeline, outcome, timing=timing, rng=event_rng, ctx=ctx,
                settings=self.settings, player_shares=self._player_shares(cfg),
                event_cache=self._event_cache, event_seed=seed,
            )
        except Exception:
            return None  # fall back to the baseline rather than crash
        prediction_market = spec.market
        prediction_params = spec.params
        if spec.market == REGULATION_STANDARD:
            baseline_spec = spec.params["baseline_spec"]
            prediction_market = baseline_spec.market.value
            prediction_params = {
                **baseline_spec.params,
                "regulation_scope": bool(spec.params.get("regulation", False)),
            }
        return Prediction(
            question=q, market=prediction_market, params=prediction_params,
            p_model=p, p_market=None, p_final=p, n_sims=outcome.n_sims,
            notes="post-sim learned event/player model (model-only)",
        )

    def _try_player_alloc(self, q, ctx, n_sims, cfg) -> Prediction | None:
        """Resolve a player count prop as a share of the simulated team total (gated, off by default)."""
        if not cfg.get("player_allocation", False):
            return None
        try:
            from sportspredict.markets import parse_question  # noqa: PLC0415
            from sportspredict.markets.schema import MarketType  # noqa: PLC0415

            from .postsim.allocation import resolve_player_stat_alloc  # noqa: PLC0415

            spec = parse_question(q, ctx)
            outcome = self._simulate(ctx, n_sims)
            shares = self._player_shares(cfg)
            if spec.market == MarketType.PLAYER_STAT:
                p = resolve_player_stat_alloc(
                    spec.params, outcome, ctx, shares, self.settings,
                    include_et=ctx.is_knockout,
                )
                if p is None:
                    return None
            elif spec.market in (MarketType.PLAYER_SCORE, MarketType.PLAYER_SCORE_OR_ASSIST):
                from .postsim.allocation import resolve_player_goal_alloc  # noqa: PLC0415

                p = resolve_player_goal_alloc(
                    spec.params, outcome, ctx, shares, self.settings,
                    include_assist=spec.market == MarketType.PLAYER_SCORE_OR_ASSIST,
                    include_et=ctx.is_knockout,
                    own_goal_share=self._timing_model(cfg).parameter("own_goal_share", 0.015),
                )
            else:
                return None
        except Exception:
            return None  # any miss -> the baseline's standalone player-stat prior handles it
        return Prediction(
            question=q, market=spec.market.value, params=spec.params,
            p_model=p, p_market=None, p_final=p, n_sims=outcome.n_sims,
            notes="post-sim allocation (share of simulated team total)",
        )

    def _player_shares(self, cfg):
        if not hasattr(self, "_player_shares_cache"):
            from .postsim.allocation import PlayerShares  # noqa: PLC0415

            path = cfg.get("player_shares")
            self._player_shares_cache = PlayerShares.load(self.settings.path(path) if path else None)
        return self._player_shares_cache

    def _timing_model(self, cfg):
        if not hasattr(self, "_timing_model_cache"):
            from .postsim.timing import TimingModel  # noqa: PLC0415

            path = cfg.get("event_model")
            self._timing_model_cache = TimingModel.load(self.settings.path(path) if path else None)
        return self._timing_model_cache

    def _timeline(self, outcome, cfg, timing=None) -> GoalTimeline:
        """Goal-minute enrichment for a simulated outcome, cached and deterministic."""
        key = id(outcome)
        tl = self._timeline_cache.get(key)
        if tl is None:
            seed = int(self.settings.seed) if self.settings.seed is not None else 0
            rng = np.random.default_rng(np.random.SeedSequence([seed, 0x71_3E]))
            tl = GoalTimeline.from_outcome(
                outcome, rng, et_minutes=float(cfg.get("et_minutes", 30.0)),
                timing=timing or self._timing_model(cfg),
            )
            self._timeline_cache[key] = tl
        return tl

    def _adjustments(self, ctx, market_odds) -> MatchAdjustments:
        cfg = self.settings.raw.get("adjust", {})
        if not cfg.get("enabled", False):
            return MatchAdjustments()

        mo: dict = {
            k: v for k, v in (market_odds or {}).items()
            if k in {"total_corners", "total_cards", "team_goals", "total_goals_mean"}
        }
        if not ({"total_corners", "total_cards", "team_goals", "total_goals_mean"} & set(mo)):
            return MatchAdjustments()  # nothing to anchor (e.g. no odds coverage) — skip the build

        base = self.rate_model.build(ctx)
        adj = adjustments_from_market(mo, {
            "goals_total": float(base.lam["goals"].sum()),
            "goals_team": [float(x) for x in base.lam["goals"].sum(axis=1)],
            "corners_total": float(base.lam["corners"].sum()),
            "yellows_total": float(base.lam["yellows"].sum()),
            "reds_total": float(base.reds.sum()),
        }, cfg)
        adj = adj.clamped(float(cfg.get("max_ratio", 2.0)))
        if not adj.is_empty():
            self.adjust_log.append({"match": f"{ctx.team_a} vs {ctx.team_b}",
                                    "rate_mult": adj.rate_mult,
                                    "sources": adj.sources})
        return adj

def build_engine(settings: Settings | None = None) -> SimulatorEngine:
    settings = settings or default_settings()
    return SimulatorEngine(settings=settings, rate_model=make_rate_model(settings))
