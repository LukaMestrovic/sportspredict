from __future__ import annotations

import math
from dataclasses import dataclass, field

from sportspredict.markets.devig import devig

ANCHORABLE = ("goals", "corners", "cards")

def _poisson_cdf(n: int, mean: float) -> float:
    term = math.exp(-mean)
    cdf = term
    for i in range(1, n + 1):
        term *= mean / i
        cdf += term
    return cdf

def _poisson_sf_halfline(line: float, mean: float) -> float:
    return 1.0 - _poisson_cdf(int(math.floor(line)), mean)

def implied_mean(lines: list[tuple[float, float]], *, lo: float = 0.1, hi: float = 30.0) -> float | None:
    pts = [(L, p) for L, p in lines if L % 1.0 == 0.5 and 0.0 < p < 1.0]
    if not pts:
        return None

    def loss(m: float) -> float:
        return sum((_poisson_sf_halfline(L, m) - p) ** 2 for L, p in pts)

    best = min((loss(lo + (hi - lo) * i / 200.0), lo + (hi - lo) * i / 200.0) for i in range(201))[1]
    step = (hi - lo) / 200.0
    a, b = max(lo, best - step), min(hi, best + step)
    for _ in range(40):
        m1, m2 = a + (b - a) / 3.0, b - (b - a) / 3.0
        if loss(m1) < loss(m2):
            b = m2
        else:
            a = m1
    return 0.5 * (a + b)

def devig_over(over: float, under: float, method: str = "shin") -> float | None:
    if not (over and under) or over <= 1.0 or under <= 1.0:
        return None
    return float(devig([over, under], method=method)[0])

def implied_mean_from_book(lines_raw: list[tuple[float, float, float]], method: str = "shin") -> float | None:
    devigged = []
    for line, over, under in lines_raw:
        p = devig_over(over, under, method)
        if p is not None:
            devigged.append((float(line), p))
    return implied_mean(devigged)

@dataclass
class MatchAdjustments:

    rate_mult: dict[str, object] = field(default_factory=dict)  # stat -> float | [mA, mB]
    sources: list[str] = field(default_factory=list)

    def is_empty(self) -> bool:
        return not self.rate_mult

    def clamped(self, max_ratio: float) -> "MatchAdjustments":
        def clamp_mult(v):
            if isinstance(v, (list, tuple)):
                return [min(max(float(x), 1.0 / max_ratio), max_ratio) for x in v]
            return min(max(float(v), 1.0 / max_ratio), max_ratio)

        return MatchAdjustments({s: clamp_mult(v) for s, v in self.rate_mult.items()},
                                list(self.sources))

    def merge(self, other: "MatchAdjustments") -> "MatchAdjustments":
        rm: dict[str, object] = dict(self.rate_mult)
        for s, v in other.rate_mult.items():
            if s in rm:
                a, b = rm[s], v
                al = a if isinstance(a, (list, tuple)) else [a, a]
                bl = b if isinstance(b, (list, tuple)) else [b, b]
                rm[s] = [al[0] * bl[0], al[1] * bl[1]]
            else:
                rm[s] = v
        return MatchAdjustments(rate_mult=rm, sources=self.sources + other.sources)

    def apply_to_context(self, ctx) -> None:
        if self.rate_mult:
            existing = dict(ctx.extra.get("rate_mult", {}))
            existing.update(self.rate_mult)
            ctx.extra["rate_mult"] = existing

def adjustments_from_market(market_odds: dict | None, model_means: dict, cfg: dict) -> MatchAdjustments:
    adj = MatchAdjustments()
    if not market_odds:
        return adj
    weights = cfg.get("anchor_weight", {})

    def shrink(target: float, model: float, w: float) -> float:
        if not model or model <= 0 or not target or target <= 0:
            return 1.0
        return float((target / model) ** w)

    tg = market_odds.get("team_goals")
    w_g = float(weights.get("goals", 0.0))
    if tg and len(tg) == 2 and model_means.get("goals_team"):
        ma, mb = model_means["goals_team"]
        adj.rate_mult["goals"] = [shrink(float(tg[0]), float(ma), w_g),
                                  shrink(float(tg[1]), float(mb), w_g)]
        adj.sources.append("odds:team_goals")
    elif market_odds.get("total_goals_mean") and model_means.get("goals_total"):
        m = shrink(float(market_odds["total_goals_mean"]), float(model_means["goals_total"]), w_g)
        adj.rate_mult["goals"] = m
        adj.sources.append("odds:total_goals")

    if market_odds.get("total_corners") and model_means.get("corners_total"):
        m = shrink(float(market_odds["total_corners"]), float(model_means["corners_total"]),
                   float(weights.get("corners", 0.0)))
        adj.rate_mult["corners"] = m
        adj.sources.append("odds:total_corners")

    if market_odds.get("total_cards") and model_means.get("yellows_total"):
        target_y = float(market_odds["total_cards"]) - float(model_means.get("reds_total", 0.0))
        if target_y > 0:
            m = shrink(target_y, float(model_means["yellows_total"]), float(weights.get("cards", 0.0)))
            adj.rate_mult["yellows"] = m
            adj.sources.append("odds:total_cards")
    return adj
