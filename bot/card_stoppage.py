"""WC2026-only calibration for stoppage-time card specials."""
from __future__ import annotations

import math
import re
from statistics import mean

from .pricing import PriceCtx


CONTRACT_KEY = "card_window:cards:stoppage_any:reg:>=:1"
AF_TOTAL_CARDS_BET_ID = 80
ODDSAPI_TOTAL_CARDS_MARKET = "alternate_totals_cards"

MIN_TRAINING_OBSERVATIONS = 30
RIDGE_STRENGTH = 0.75
MAX_ABS_BETA = 0.18
MAX_ABS_LOGIT_ADJUSTMENT = 0.25


def fit_model(rows: list[dict]) -> dict:
    """Fit a conservative one-feature model from settled WC2026 fixtures.

    The intercept is fixed at the current WC2026 empirical rate. The only
    learned parameter is the slope from total regulation cards to the
    stoppage-card logit, strongly regularized and capped at prediction time.
    """
    clean = _clean_rows(rows)
    n = len(clean)
    if not clean:
        return {
            "available": False,
            "reason": "No WC2026 stoppage-card rows had both labels and card totals.",
            "training_scope": "wc2026_settled_before_target",
            "observations": 0,
        }

    yes_events = sum(row["outcome"] for row in clean)
    empirical_rate = yes_events / n
    mean_cards = mean(row["total_cards"] for row in clean)
    beta = 0.0
    reason = "Insufficient WC2026 observations; using empirical rate only."
    available = n >= MIN_TRAINING_OBSERVATIONS
    if available and 0 < yes_events < n and _variance([r["total_cards"] for r in clean]) > 0:
        beta = _fit_beta(clean, empirical_rate, mean_cards)
        reason = (
            "WC2026-only logistic card-count adjustment centered on the current "
            "tournament empirical rate."
        )
    elif available:
        reason = "WC2026 labels have no usable outcome/card-count variation."

    empirical_predictions = [empirical_rate for _row in clean]
    model_predictions = [
        _predict_probability(empirical_rate, beta, mean_cards, row["total_cards"])[0]
        for row in clean
    ]
    outcomes = [row["outcome"] for row in clean]
    return {
        "available": available,
        "reason": reason,
        "contract_key": CONTRACT_KEY,
        "training_scope": "wc2026_settled_before_target",
        "observations": n,
        "yes_events": int(yes_events),
        "empirical_rate": round(empirical_rate, 6),
        "mean_total_cards": round(mean_cards, 6),
        "beta": round(beta, 6),
        "min_training_observations": MIN_TRAINING_OBSERVATIONS,
        "regularization": {
            "ridge_strength": RIDGE_STRENGTH,
            "max_abs_beta": MAX_ABS_BETA,
            "max_abs_logit_adjustment": MAX_ABS_LOGIT_ADJUSTMENT,
        },
        "brier": {
            "empirical_rate": round(_brier(empirical_predictions, outcomes), 6),
            "card_count_model": round(_brier(model_predictions, outcomes), 6),
            "always_50": round(_brier([0.5 for _row in clean], outcomes), 6),
        },
    }


def predict_from_model(model: dict, expected_total_cards: float | None) -> dict | None:
    """Return the centered card-count prediction payload for a target match."""
    empirical_rate = _probability(model.get("empirical_rate"))
    if empirical_rate is None:
        return None
    mean_cards = _float(model.get("mean_total_cards"))
    beta = _float(model.get("beta")) or 0.0
    if expected_total_cards is None or mean_cards is None:
        return {
            "probability": empirical_rate,
            "base_probability": empirical_rate,
            "logit_adjustment": 0.0,
            "raw_logit_adjustment": 0.0,
            "reason": "No expected total-card signal was available; using WC2026 empirical rate.",
        }
    probability, raw_adjustment, adjustment = _predict_probability(
        empirical_rate, beta, mean_cards, expected_total_cards,
    )
    return {
        "probability": probability,
        "base_probability": empirical_rate,
        "expected_total_cards": expected_total_cards,
        "mean_training_total_cards": mean_cards,
        "beta": beta,
        "raw_logit_adjustment": round(raw_adjustment, 6),
        "logit_adjustment": round(adjustment, 6),
        "max_abs_logit_adjustment": MAX_ABS_LOGIT_ADJUSTMENT,
        "reason": (
            "WC2026 empirical rate adjusted by expected total cards, with the "
            "logit move capped."
        ),
    }


def expected_total_cards_from_context(ctx: PriceCtx) -> dict | None:
    """Infer expected regulation cards from available total-card O/U odds."""
    from_af = expected_total_cards_from_af_books(ctx.af_books or [])
    if from_af:
        return from_af
    if not ctx.oa or not ctx.oa_event:
        return None
    books = ctx.oa.event_odds(ctx.oa_event["id"], [ODDSAPI_TOTAL_CARDS_MARKET])
    return expected_total_cards_from_oddsapi_books(books)


def expected_total_cards_from_af_books(bookmakers: list[dict]) -> dict | None:
    """Infer total-card lambda from API-Football total-card O/U prices."""
    candidates: dict[float, list[tuple[float, float, str]]] = {}
    for bookmaker in bookmakers or []:
        bet = next(
            (item for item in bookmaker.get("bets", [])
             if item.get("id") == AF_TOTAL_CARDS_BET_ID),
            None,
        )
        if not bet:
            continue
        values = bet.get("values") or []
        for line in _af_lines(values):
            probability = _af_over_probability(values, line)
            if probability is None:
                continue
            lam = _lambda_for_tail(int(line + 0.5), probability)
            candidates.setdefault(line, []).append((
                lam, probability, bookmaker.get("name") or "unknown",
            ))
    return _expected_payload(
        candidates,
        source="api-football",
        market_key=f"af_bet_{AF_TOTAL_CARDS_BET_ID}",
    )


def expected_total_cards_from_oddsapi_books(bookmakers: list[dict]) -> dict | None:
    """Infer total-card lambda from Odds API alternate total-card prices."""
    candidates: dict[float, list[tuple[float, float, str]]] = {}
    for bookmaker in bookmakers or []:
        for market in bookmaker.get("markets") or []:
            if market.get("key") != ODDSAPI_TOTAL_CARDS_MARKET:
                continue
            outcomes = market.get("outcomes") or []
            for line in _oddsapi_lines(outcomes):
                probability = _oddsapi_over_probability(outcomes, line)
                if probability is None:
                    continue
                lam = _lambda_for_tail(int(line + 0.5), probability)
                candidates.setdefault(line, []).append((
                    lam, probability,
                    bookmaker.get("title") or bookmaker.get("key") or "unknown",
                ))
    return _expected_payload(
        candidates,
        source="odds-api",
        market_key=ODDSAPI_TOTAL_CARDS_MARKET,
    )


def _clean_rows(rows: list[dict]) -> list[dict]:
    clean = []
    for row in rows or []:
        total_cards = _float(row.get("total_cards"))
        if total_cards is None:
            continue
        outcome = row.get("outcome")
        if outcome is None:
            continue
        clean.append({
            "total_cards": total_cards,
            "outcome": 1 if bool(outcome) else 0,
        })
    return clean


def _fit_beta(rows: list[dict], empirical_rate: float, mean_cards: float) -> float:
    best_beta = 0.0
    best_score = float("inf")
    steps = int((2 * MAX_ABS_BETA) / 0.001) + 1
    base_logit = _logit(empirical_rate)
    for index in range(steps + 1):
        beta = -MAX_ABS_BETA + index * 0.001
        logloss = 0.0
        for row in rows:
            raw_adjustment = beta * (row["total_cards"] - mean_cards)
            adjustment = _clamp(
                raw_adjustment, -MAX_ABS_LOGIT_ADJUSTMENT,
                MAX_ABS_LOGIT_ADJUSTMENT,
            )
            probability = _sigmoid(base_logit + adjustment)
            logloss += _logloss(probability, row["outcome"])
        score = logloss / len(rows) + RIDGE_STRENGTH * beta * beta
        if score < best_score:
            best_score = score
            best_beta = beta
    return best_beta


def _predict_probability(
    empirical_rate: float,
    beta: float,
    mean_cards: float,
    cards: float,
) -> tuple[float, float, float]:
    raw_adjustment = beta * (cards - mean_cards)
    adjustment = _clamp(
        raw_adjustment, -MAX_ABS_LOGIT_ADJUSTMENT, MAX_ABS_LOGIT_ADJUSTMENT,
    )
    return _sigmoid(_logit(empirical_rate) + adjustment), raw_adjustment, adjustment


def _expected_payload(
    candidates: dict[float, list[tuple[float, float, str]]],
    *,
    source: str,
    market_key: str,
) -> dict | None:
    if not candidates:
        return None
    line, rows = max(
        candidates.items(),
        key=lambda item: (len(item[1]), -abs(item[0] - 4.5), -item[0]),
    )
    lambdas = [row[0] for row in rows]
    probabilities = [row[1] for row in rows]
    bookmakers = [row[2] for row in rows]
    return {
        "source": source,
        "market_key": market_key,
        "line": line,
        "book_count": len(rows),
        "bookmakers": bookmakers[:12],
        "over_probability": round(mean(probabilities), 6),
        "expected_total_cards": round(mean(lambdas), 6),
        "devig_method": "same-book over/under de-vig plus Poisson-tail inversion",
    }


def _af_lines(values: list[dict]) -> set[float]:
    lines = set()
    for value in values:
        line = _line_from_text(value.get("value"))
        if line is not None:
            lines.add(line)
    return lines


def _af_over_probability(values: list[dict], line: float) -> float | None:
    over = under = None
    for value in values:
        raw_line = _line_from_text(value.get("value"))
        if raw_line is None or abs(raw_line - line) > 1e-6:
            continue
        implied = _implied(value.get("odd"))
        if implied is None:
            continue
        label = str(value.get("value") or "").strip().lower()
        if label.startswith("over"):
            over = implied
        elif label.startswith("under"):
            under = implied
    if over is None or under is None or over + under <= 0:
        return None
    return over / (over + under)


def _oddsapi_lines(outcomes: list[dict]) -> set[float]:
    lines = set()
    for outcome in outcomes:
        point = _float(outcome.get("point"))
        if point is not None and _is_half_line(point):
            lines.add(point)
    return lines


def _oddsapi_over_probability(outcomes: list[dict], line: float) -> float | None:
    over = under = None
    for outcome in outcomes:
        point = _float(outcome.get("point"))
        if point is None or abs(point - line) > 1e-6:
            continue
        implied = _implied(outcome.get("price"))
        if implied is None:
            continue
        name = str(outcome.get("name") or "").strip().lower()
        if name == "over":
            over = implied
        elif name == "under":
            under = implied
    if over is None or under is None or over + under <= 0:
        return None
    return over / (over + under)


def _line_from_text(value: object) -> float | None:
    match = re.search(r"(\d+(?:\.\d+)?)", str(value or ""))
    if not match:
        return None
    line = float(match.group(1))
    return line if _is_half_line(line) else None


def _is_half_line(value: float) -> bool:
    return abs((value % 1.0) - 0.5) < 1e-6


def _lambda_for_tail(threshold: int, probability: float) -> float:
    probability = _clamp(probability, 0.001, 0.999)
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


def _implied(price: object) -> float | None:
    try:
        return 1.0 / float(price)
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def _probability(value: object) -> float | None:
    number = _float(value)
    if number is None or not 0 < number < 1:
        return None
    return number


def _float(value: object) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _variance(values: list[float]) -> float:
    if not values:
        return 0.0
    avg = mean(values)
    return mean((value - avg) ** 2 for value in values)


def _brier(predictions: list[float], outcomes: list[int]) -> float:
    return mean((prediction - outcome) ** 2 for prediction, outcome in zip(predictions, outcomes))


def _logloss(probability: float, outcome: int) -> float:
    probability = _clamp(probability, 0.000001, 0.999999)
    return (
        -math.log(probability)
        if outcome
        else -math.log(1.0 - probability)
    )


def _logit(probability: float) -> float:
    probability = _clamp(probability, 0.000001, 0.999999)
    return math.log(probability / (1.0 - probability))


def _sigmoid(value: float) -> float:
    return 1.0 / (1.0 + math.exp(-value))


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))
