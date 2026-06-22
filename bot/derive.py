"""Layer 3 — derive a probability from related/component markets.

When a question has no single matching market, we decompose it and combine the
component probabilities. The dominant case is compound questions:

  "Will both teams score AND the match have 3+ goals?"   (A AND B)
  "Will a penalty be awarded OR a red card be shown?"    (A OR B)

We split the compound into two atomic questions (LLM), price each through the
normal cascade ([pricing.price_intent]), and combine assuming independence:

  P(A AND B) = P(A) · P(B)
  P(A OR  B) = P(A) + P(B) − P(A)·P(B)

Independence is an approximation (components are often correlated), but it is
auditable and far better than skipping — see README.
"""
from __future__ import annotations

import json
import math
import re
from statistics import mean

from . import predictor as afpred
from .parser import chat_json, parse_questions
from .pricing import PriceCtx, price_intent


SHOT_HALF_SHARE = {"1H": 0.45, "2H": 0.55, "match": 1.0}
CARD_SECOND_HALF_SHARE = 0.58
SHOT_LOGIT_INTERCEPT = -0.18

# A team always fields eleven, so the team's shot timing (more shots late, 0.55
# in H2) is the right split for team/total half markets. An *individual* is not:
# players get substituted, and subs cluster in the second half, so a starter is
# far more reliably on the pitch in H1. We therefore weight each half by team
# shot-timing × expected on-pitch fraction (≈1.0 in H1, ≈0.8 in H2 for a typical
# starter), then renormalise so the two halves still partition the full-match
# rate. This pulls a player's H2 share down from 0.55 to ≈0.49 and corrects the
# systematic over-pricing of "player does X in the 2nd half", which otherwise
# silently assumes a full 90 minutes. (Without lineups we can't tell a starter
# from a sub; this is the conservative population-average correction.)
_PLAYER_ON_PITCH = {"1H": 1.0, "2H": 0.80}
PLAYER_HALF_SHARE = {
    h: (SHOT_HALF_SHARE[h] * _PLAYER_ON_PITCH[h])
    / sum(SHOT_HALF_SHARE[k] * _PLAYER_ON_PITCH[k] for k in ("1H", "2H"))
    for h in ("1H", "2H")
}

_SPLIT_SYS = """Split a compound soccer betting question into its two atomic
sub-questions and the logical operator joining them. Each sub-question must be a
standalone yes/no question. Return JSON:
{"op": "AND"|"OR", "a": "<sub-question 1>", "b": "<sub-question 2>"}
If it is not actually a compound of two events, return {"op": null}."""


def _split(question: str) -> dict | None:
    content = chat_json([{"role": "system", "content": _SPLIT_SYS},
                         {"role": "user", "content": question}])
    out = json.loads(content)
    return out if out.get("op") in ("AND", "OR") else None


def _price_sub(question: str, ctx: PriceCtx) -> float | None:
    intent = parse_questions([{"id": "x", "question": question}], ctx.home, ctx.away).get("x")
    if not intent:
        return None
    out, _src, _spec = price_intent(intent, ctx)
    return out["probability"] if out else None


def price_compound(question: str, ctx: PriceCtx):
    """Return (out, source) or (None, None)."""
    split = _split(question)
    if not split:
        return None, None
    pa = _price_sub(split["a"], ctx)
    pb = _price_sub(split["b"], ctx)
    if pa is None or pb is None:
        return None, None
    p = pa * pb if split["op"] == "AND" else pa + pb - pa * pb
    label = f"{split['op']}({pa:.2f},{pb:.2f})"
    return {"probability": p, "n_books": 0, "label": label}, "derived"


def price_empirical(question: str, intent: dict | None, ctx: PriceCtx):
    """Estimate an unsupported contract from related API-Football markets.

    These formulas deliberately use only ``ctx.af_books``. They never spend
    Odds API credits and return ``None`` when the required signal is absent.
    """
    intent = intent or {}
    market = intent.get("market")
    period = intent.get("period", "match")
    lower = question.lower()

    # "More shots on target than the opponent in the 1st/2nd half" has no
    # bookmaker market (the full-match SoT 1x2, bet 176, is priced directly by
    # API-Football). Route it to the team-shots "more" model, splitting the
    # match rate into the half. The match-period case never reaches here.
    if market == "shots_on_target_compare" and period in ("1H", "2H"):
        market = "team_shots_on_target"
        intent = {**intent, "comparator": "more"}

    if "penalty kick be awarded" in lower:
        penalty = _penalty_awarded(ctx)
        if penalty is None:
            return None, None
        if "red card" in lower:
            red = _red_card(ctx)
            if red is None:
                return None, None
            # The events are positively correlated, so their intersection is
            # larger than under independence and the union is slightly smaller.
            p = penalty + red - 1.35 * penalty * red
            label = f"empirical penalty-or-red ({penalty:.2f},{red:.2f})"
        else:
            p, label = penalty, f"empirical penalty awarded ({penalty:.2f})"
        return _empirical_out(p, label)

    if "both teams" in lower and "shot on target" in lower:
        model = _shot_model(ctx)
        if not model:
            return None, None
        share = SHOT_HALF_SHARE.get(period, 1.0)
        p = (1 - math.exp(-model[0] * share)) * (1 - math.exp(-model[1] * share))
        return _empirical_out(p, f"empirical both teams SoT {period}")

    if market == "team_shots_on_target":
        model = _shot_model(ctx)
        if not model or intent.get("subject") not in ("home", "away"):
            return None, None
        share = SHOT_HALF_SHARE.get(period, 1.0)
        if intent.get("comparator") == "more":
            p = _poisson_more(model[0] * share, model[1] * share)
            if intent["subject"] == "away":
                p = _poisson_more(model[1] * share, model[0] * share)
        else:
            p = _calibrate_shot_probability(_count_probability(
                model[0 if intent["subject"] == "home" else 1] * share, intent
            ))
        return _empirical_out(p, f"empirical team SoT {period}")

    if market == "total_shots_on_target" and period in ("1H", "2H"):
        model = _shot_model(ctx)
        if not model:
            return None, None
        p = _calibrate_shot_probability(
            _count_probability(sum(model) * SHOT_HALF_SHARE[period], intent)
        )
        return _empirical_out(p, f"empirical total SoT {period}")

    if market == "player_shots_on_target" and period in ("1H", "2H"):
        full = _af_price(ctx, {
            **intent, "period": "match",
        })
        if full is None or intent.get("threshold") is None:
            return None, None
        threshold = int(intent["threshold"])
        # `full` is P(X>=T) for gte but P(X<=T) for lte; invert from the tail
        # that actually matches the contract, else the recovered rate is wrong.
        if intent.get("comparator") == "lte":
            lam = _lambda_for_tail(threshold + 1, 1 - full)
        else:
            lam = _lambda_for_tail(threshold, full)
        # Player half share (minutes-aware), not the team's: see PLAYER_HALF_SHARE.
        p = _count_probability(lam * PLAYER_HALF_SHARE[period], intent)
        return _empirical_out(p, f"empirical player SoT {period}")

    # "Player scores in the 1st/2nd half" has no bookmaker line; the full-match
    # anytime-scorer prop (bet 92) does. Convert it to a goal rate, scale by the
    # minutes-aware half share, and recompute P(>=1 goal in the half).
    if (market == "player_goal_scorer" and period in ("1H", "2H")
            and intent.get("player")):
        full = _af_price(ctx, {**intent, "period": "match"})
        if full is None:
            return None, None
        lam = -math.log1p(-full) * PLAYER_HALF_SHARE[period]
        return _empirical_out(1 - math.exp(-lam), f"empirical player scores {period}")

    if market == "team_cards" and period in ("1H", "2H"):
        model = _card_model(ctx, period)
        if not model or intent.get("subject") not in ("home", "away"):
            return None, None
        p = _count_probability(model[0 if intent["subject"] == "home" else 1], intent)
        return _empirical_out(p, f"empirical team cards {period}")

    if "first goal of the second half" in lower:
        subject = intent.get("subject")
        if subject not in ("home", "away"):
            return None, None
        ph = _af_price(ctx, _yes_intent("team_score_2h", "home", "2H"))
        pa = _af_price(ctx, _yes_intent("team_score_2h", "away", "2H"))
        if ph is None or pa is None:
            return None, None
        lh, la = -math.log1p(-ph), -math.log1p(-pa)
        total = lh + la
        p = (1 - math.exp(-total)) * (lh if subject == "home" else la) / total
        return _empirical_out(p, "empirical first scorer 2H")

    return None, None


def _shot_model(ctx: PriceCtx) -> tuple[float, float] | None:
    total = _infer_total_rate(ctx.af_books, 87)
    if total is None:
        return None
    home_more = _select_probability(ctx, 176, "Home")
    away_more = _select_probability(ctx, 176, "Away")
    if home_more is None or away_more is None:
        home_more = _select_probability(ctx, 1, "Home")
        away_more = _select_probability(ctx, 1, "Away")
    if home_more is None or away_more is None:
        return None
    home_share = _clamp(0.5 + 0.40 * (home_more - away_more), 0.20, 0.80)
    return total * home_share, total * (1 - home_share)


def _card_model(ctx: PriceCtx, period: str) -> tuple[float, float] | None:
    total_bet = 155 if period == "1H" else 156
    compare_bet = 161 if period == "1H" else 162
    total = _infer_total_rate(ctx.af_books, total_bet)
    if total is not None:
        home_more = _select_probability(ctx, compare_bet, "Home")
        away_more = _select_probability(ctx, compare_bet, "Away")
        if home_more is not None and away_more is not None:
            home_share = _clamp(0.5 + 0.40 * (home_more - away_more), 0.20, 0.80)
            return total * home_share, total * (1 - home_share)
    home = _infer_total_rate(ctx.af_books, 82)
    away = _infer_total_rate(ctx.af_books, 83)
    if home is None or away is None:
        return None
    share = 1 - CARD_SECOND_HALF_SHARE if period == "1H" else CARD_SECOND_HALF_SHARE
    return home * share, away * share


def _infer_total_rate(bookmakers: list[dict], bet_id: int) -> float | None:
    lines: set[float] = set()
    for bookmaker in bookmakers:
        bet = next((b for b in bookmaker.get("bets", []) if b.get("id") == bet_id), None)
        for value in bet.get("values", []) if bet else []:
            match = re.search(r"(\d+(?:\.\d+)?)", value.get("value", ""))
            if match and float(match.group(1)) % 1 == 0.5:
                lines.add(float(match.group(1)))
    candidates = []
    for line in lines:
        out = afpred.predict(bookmakers, {
            "type": "ou", "bet_id": bet_id, "side": "Over", "line": line,
            "label": "empirical signal",
        })
        if out:
            candidates.append((out["n_books"], line, out["probability"]))
    if not candidates:
        return None
    _books, line, probability = max(candidates)
    return _lambda_for_tail(int(line + 0.5), probability)


def _select_probability(ctx: PriceCtx, bet_id: int, value: str) -> float | None:
    out = afpred.predict(ctx.af_books, {
        "type": "select", "bet_id": bet_id, "value": value,
        "label": "empirical signal",
    })
    return out["probability"] if out else None


def _af_price(ctx: PriceCtx, intent: dict) -> float | None:
    from .matcher import match_intent
    spec = match_intent(intent, ctx.home, ctx.away)
    out = afpred.predict(ctx.af_books, spec) if spec else None
    return out["probability"] if out else None


def _penalty_awarded(ctx: PriceCtx) -> float | None:
    scored = _single_sided_event(ctx.af_books, 99, {"home", "away"})
    if scored is not None:
        return _clamp(scored / 0.80, 0.03, 0.35)
    cards = _af_price(ctx, {
        "market": "total_cards", "subject": "match", "comparator": "gte",
        "threshold": 4, "period": "match",
    })
    goals = _af_price(ctx, {
        "market": "total_goals", "subject": "match", "comparator": "gte",
        "threshold": 3, "period": "match",
    })
    if cards is None or goals is None:
        return None
    return _clamp(0.19 + 0.08 * (cards - 0.5) + 0.04 * (goals - 0.5), 0.08, 0.30)


def _red_card(ctx: PriceCtx) -> float | None:
    direct = _single_sided_event(ctx.af_books, 86, {"yes"})
    if direct is not None:
        return _clamp(direct, 0.02, 0.20)
    cards = _af_price(ctx, {
        "market": "total_cards", "subject": "match", "comparator": "gte",
        "threshold": 4, "period": "match",
    })
    return _clamp(0.08 + 0.08 * (cards - 0.5), 0.03, 0.16) if cards is not None else None


def _single_sided_event(
    bookmakers: list[dict], bet_id: int, accepted_values: set[str]
) -> float | None:
    probabilities = []
    for bookmaker in bookmakers:
        bet = next((b for b in bookmaker.get("bets", []) if b.get("id") == bet_id), None)
        if not bet:
            continue
        implied = 0.0
        for value in bet.get("values", []):
            if value.get("value", "").strip().lower() not in accepted_values:
                continue
            try:
                implied += 1 / float(value["odd"])
            except (TypeError, ValueError, ZeroDivisionError):
                pass
        if implied:
            probabilities.append(min(0.99, implied * 0.90))
    return mean(probabilities) if probabilities else None


def _count_probability(lam: float, intent: dict) -> float:
    """P(count satisfies the intent) under Poisson(lam).

    Mirrors the bookmaker convention (parser "N or fewer"; matcher
    _line_from_threshold): gte T -> P(X >= T); lte T -> P(X <= T), inclusive of
    T. P(X <= T) = 1 - P(X >= T+1), so the lte tail starts one bucket higher.
    """
    threshold = int(intent.get("threshold") or 1)
    if intent.get("comparator") == "lte":
        return 1 - _poisson_tail(lam, threshold + 1)
    return _poisson_tail(lam, threshold)


def _calibrate_shot_probability(probability: float) -> float:
    probability = _clamp(probability, 1e-6, 1 - 1e-6)
    logit = math.log(probability / (1 - probability)) + SHOT_LOGIT_INTERCEPT
    return 1 / (1 + math.exp(-logit))


def _lambda_for_tail(threshold: int, probability: float) -> float:
    low, high = 0.001, 40.0
    for _ in range(60):
        mid = (low + high) / 2
        if _poisson_tail(mid, threshold) < probability:
            low = mid
        else:
            high = mid
    return (low + high) / 2


def _poisson_tail(lam: float, threshold: int) -> float:
    if threshold <= 0:
        return 1.0
    term = math.exp(-lam)
    cdf = term
    for k in range(1, threshold):
        term *= lam / k
        cdf += term
    return _clamp(1 - cdf, 0.0, 1.0)


def _poisson_more(left: float, right: float) -> float:
    max_count = max(25, int(left + right + 10 * math.sqrt(left + right + 1)))
    right_cdf = 0.0
    right_term = math.exp(-right)
    left_term = math.exp(-left)
    probability = 0.0
    for count in range(max_count + 1):
        if count:
            right_term *= right / count
            left_term *= left / count
        probability += left_term * right_cdf
        right_cdf += right_term
    return _clamp(probability, 0.0, 1.0)


def _yes_intent(market: str, subject: str, period: str) -> dict:
    return {"market": market, "subject": subject, "comparator": "yes",
            "threshold": None, "period": period}


def _empirical_out(probability: float, label: str):
    probability = _clamp(probability, 0.01, 0.99)
    return {"probability": probability, "n_books": 0, "label": label}, "empirical"


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))
