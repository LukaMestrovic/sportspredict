from __future__ import annotations

import dataclasses
from pathlib import Path

import numpy as np

from sportspredict.rates import MatchRates, RateModel
from sportspredict.types import PER_HALF_STATS

from .team_ratings import TeamRatings, load_team_ratings

# The learned model's feature vector. One row per team (the simulator works per-team/per-half),
# built from Elo strength/context plus opponent-adjusted attack/defense ratings.
FEATURE_ORDER: tuple[str, ...] = (
    "z", "own_elo_c", "opp_elo_c", "own_attack", "own_defense", "opp_attack",
    "opp_defense", "is_knockout", "own_host", "opp_host",
)

def _row_features(own_elo: float, opp_elo: float, own_team: str, opp_team: str,
                  is_knockout: bool, own_host: bool, opp_host: bool,
                  ratings: TeamRatings) -> np.ndarray:
    own_a, own_d = ratings.get(own_team)
    opp_a, opp_d = ratings.get(opp_team)
    return np.array(
        [
            (own_elo - opp_elo) / 100.0,
            (own_elo - 1500.0) / 100.0,
            (opp_elo - 1500.0) / 100.0,
            own_a,
            own_d,
            opp_a,
            opp_d,
            1.0 if is_knockout else 0.0,
            1.0 if own_host else 0.0,
            1.0 if opp_host else 0.0,
        ],
        dtype=float,
    )

def build_features(ctx, ratings: TeamRatings) -> np.ndarray:
    return np.vstack([
        _row_features(ctx.elo_a, ctx.elo_b, ctx.team_a, ctx.team_b,
                      ctx.is_knockout, ctx.host_a, ctx.host_b, ratings),
        _row_features(ctx.elo_b, ctx.elo_a, ctx.team_b, ctx.team_a,
                      ctx.is_knockout, ctx.host_b, ctx.host_a, ratings),
    ])

def apply_ctx_rate_mult(base_rates: MatchRates, ctx, max_ratio: float) -> MatchRates:
    """Apply bounded per-stat lambda multipliers from ``ctx.extra['rate_mult']`` (odds anchors)."""
    rm = getattr(ctx, "extra", {}).get("rate_mult") if hasattr(ctx, "extra") else None
    if not rm:
        return base_rates
    new_lam = dict(base_rates.lam)
    changed = False
    for stat, mult in rm.items():
        if stat not in new_lam or stat not in PER_HALF_STATS:
            continue
        pair = mult if isinstance(mult, (list, tuple)) else (mult, mult)
        arr = np.clip(np.array([float(pair[0]), float(pair[1])]), 1.0 / max_ratio, max_ratio)
        if np.allclose(arr, 1.0):
            continue
        new_lam[stat] = new_lam[stat] * arr[:, None]  # (2,1) broadcasts over halves
        changed = True
    return dataclasses.replace(base_rates, lam=new_lam) if changed else base_rates

class LearnedRateModel(RateModel):
    """Gray-box Layer-1 rates: call the baseline RateModel, then blend each stat's per-match mean
    toward a per-stat GBM correction (capped by ``alpha``/``max_ratio``), then apply odds anchors."""

    def __init__(
        self,
        settings,
        *,
        gbms: dict,
        feature_names: tuple[str, ...],
        alphas: dict[str, float],
        team_ratings: TeamRatings | None = None,
        max_alpha: float = 1.0,
        max_ratio: float | None = None,
        target: str = "absolute",
    ) -> None:
        super().__init__(settings)
        if tuple(feature_names) != FEATURE_ORDER:
            raise ValueError(
                f"feature order mismatch: artifact {tuple(feature_names)} != {FEATURE_ORDER}"
            )
        self._gbms = dict(gbms)
        self._target = str(target)
        self._max_alpha = float(max_alpha)
        self._max_ratio = float(max_ratio) if max_ratio and max_ratio > 1.0 else None
        self._alphas = {s: min(float(a), self._max_alpha) for s, a in alphas.items()}
        self._team_ratings = team_ratings or TeamRatings.neutral()

    @classmethod
    def from_settings(cls, settings) -> "LearnedRateModel | None":
        cfg = settings.raw.get("rates", {}).get("learned", {})
        artifact = cfg.get("artifact", "data/processed/rate_model.joblib")
        path = settings.path(artifact)
        if not Path(path).exists():
            return None
        import joblib

        bundle = joblib.load(path)
        if tuple(bundle.get("feature_names", ())) != FEATURE_ORDER:
            return None
        ratings_path = cfg.get("team_ratings")
        ratings = load_team_ratings(settings.path(ratings_path) if ratings_path else None)
        default_alpha = float(cfg.get("blend_alpha", 0.5))
        max_alpha = float(cfg.get("max_alpha", 1.0))
        max_ratio = cfg.get("max_ratio")
        gbms = bundle["gbms"]
        alphas = {s: default_alpha for s in gbms}
        alphas.update(bundle.get("alphas", {}))
        learned_stats = set(cfg.get("learned_stats", list(gbms)))
        gbms = {s: m for s, m in gbms.items() if s in learned_stats}
        return cls(
            settings,
            gbms=gbms,
            feature_names=bundle["feature_names"],
            alphas=alphas,
            team_ratings=ratings,
            max_alpha=max_alpha,
            max_ratio=float(max_ratio) if max_ratio else None,
            target=str(bundle.get("target", "absolute")),
        )

    def _learned_per_match(self, ctx) -> dict[str, np.ndarray]:
        X = build_features(ctx, self._team_ratings)
        return {stat: np.clip(np.asarray(gbm.predict(X), dtype=float), 1e-6, None)
                for stat, gbm in self._gbms.items()}

    def build(self, ctx) -> MatchRates:
        base_rates = super().build(ctx)
        anchor_max = self._max_ratio or 3.0
        if not self._gbms:
            return apply_ctx_rate_mult(base_rates, ctx, anchor_max)

        learned = self._learned_per_match(ctx)
        new_lam = dict(base_rates.lam)
        changed = False
        for stat, learned_total in learned.items():
            if stat not in new_lam or stat not in PER_HALF_STATS:
                continue
            alpha = float(self._alphas.get(stat, 0.0))
            if alpha <= 0.0:
                continue
            base_total = np.clip(base_rates.lam[stat].sum(axis=1), 1e-9, None)
            lt = base_total * learned_total if self._target == "ratio_to_baseline" else learned_total
            if self._max_ratio is not None:
                lt = np.clip(lt, base_total / self._max_ratio, base_total * self._max_ratio)
            blended = np.exp((1.0 - alpha) * np.log(base_total) + alpha * np.log(lt))
            new_lam[stat] = base_rates.lam[stat] * (blended / base_total)[:, None]
            changed = True

        rates = dataclasses.replace(base_rates, lam=new_lam) if changed else base_rates
        return apply_ctx_rate_mult(rates, ctx, anchor_max)
