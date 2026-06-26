"""Predictor: bookmaker odds -> de-vigged probability for a matched market.

De-vig rule (per integration checklist): only normalize coherent outcome sets
from the SAME bookmaker and contract. We de-vig per bookmaker, then average the
fair probability across all bookmakers that quote the contract.
"""
from __future__ import annotations

import re
from statistics import mean

from .teams import player_matches


# Single-sided (player-prop) quotes carry only the YES leg, so we cannot de-vig
# against a coherent NO. Player props book a much heavier margin than the 1X2 /
# totals lines — and it is concentrated on the popular YES side — so a flat 8%
# haircut left the bot over-pricing "player to do X" (settled audit: player
# shots-on-target YES biased +0.10, the crowd +0.08). 0.85 reflects the ~15%
# single-side overround typical of player props; it is anchored to that market
# convention, not fit to the sample, and only touches player-prop pricing.
SINGLE_SIDE_DEVIG = 0.85


def _parse_line(value: str) -> float | None:
    m = re.search(r"(\d+\.?\d*)", value)
    return float(m.group(1)) if m else None


def _bets_by_id(bookmaker: dict, bet_id: int) -> dict | None:
    for bet in bookmaker["bets"]:
        if bet["id"] == bet_id:
            return bet
    return None


def _devig_select(values: list[dict], target: str) -> float | None:
    """Normalize a full categorical outcome set, return target's fair prob."""
    implied = []
    target_imp = None
    for v in values:
        try:
            imp = 1.0 / float(v["odd"])
        except (ValueError, ZeroDivisionError):
            continue
        implied.append(imp)
        if v["value"].strip().lower() == target.strip().lower():
            target_imp = imp
    total = sum(implied)
    if target_imp is None or total <= 0:
        return None
    return target_imp / total


def _devig_ou(values: list[dict], side: str, line: float) -> float | None:
    """De-vig the Over/Under pair at a specific line."""
    over = under = None
    for v in values:
        txt = v["value"].strip()
        ln = _parse_line(txt)
        if ln is None or abs(ln - line) > 1e-6:
            continue
        try:
            imp = 1.0 / float(v["odd"])
        except (ValueError, ZeroDivisionError):
            continue
        if txt.lower().startswith("over"):
            over = imp
        elif txt.lower().startswith("under"):
            under = imp
    if over is None or under is None:
        return None
    fair_over = over / (over + under)
    return fair_over if side == "Over" else 1 - fair_over


def _single_side_probability(odd) -> float | None:
    try:
        return min(0.99, (1.0 / float(odd)) * SINGLE_SIDE_DEVIG)
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def _price_player_yes(values: list[dict], player: str) -> float | None:
    if not player:
        return None
    value = next((v for v in values if player_matches(v.get("value", ""), player)), None)
    return _single_side_probability(value["odd"]) if value else None


def _price_player_threshold(
    values: list[dict], player: str, side: str, line: float
) -> float | None:
    """Price API-Football values shaped like ``Player Name - 1+``."""
    if not player or line < 0 or not float(line + 0.5).is_integer():
        return None
    target = int(line + 0.5)
    for value in values:
        match = re.fullmatch(r"(.+?)\s*-\s*(\d+)\+", value.get("value", "").strip())
        if not match or int(match.group(2)) != target:
            continue
        if player_matches(match.group(1), player):
            over = _single_side_probability(value.get("odd"))
            if over is None:
                return None
            return over if side == "Over" else 1 - over
    return None


def predict(bookmakers: list[dict], spec: dict) -> dict | None:
    """Return {probability: float 0-1, n_books: int, label} or None to skip."""
    probs: list[float] = []
    for bm in bookmakers:
        bet = _bets_by_id(bm, spec["bet_id"])
        if not bet:
            continue
        if spec["type"] == "select":
            p = _devig_select(bet["values"], spec["value"])
        elif spec["type"] == "ou":
            p = _devig_ou(bet["values"], spec["side"], spec["line"])
        elif spec["type"] == "player_yes":
            p = _price_player_yes(bet["values"], spec.get("player"))
        elif spec["type"] == "player_threshold":
            p = _price_player_threshold(bet["values"], spec.get("player"),
                                        spec["side"], spec["line"])
        else:
            p = None
        if p is not None and 0.0 < p < 1.0:
            probs.append(p)
    if not probs:
        return None
    p = mean(probs)
    # A lone book on an AF market usually means a mis-mapped/odd contract; trust
    # it only if it isn't an extreme (extreme + thin = the unreliable case).
    if (spec["type"] not in ("player_yes", "player_threshold")
            and len(probs) < 2 and (p > 0.9 or p < 0.1)):
        return None
    return {"probability": p, "n_books": len(probs),
            "book_probabilities": probs,
            "label": spec.get("label", "")}


def observations(bookmakers: list[dict], spec: dict | None) -> list[dict]:
    """Per-book fair-probability observations for an API-Football spec.

    This mirrors ``predict`` but keeps the bookmaker name, raw contract odds and
    de-vig method so the LLM pricing layer can audit exactly which bookmaker
    probabilities it saw instead of only receiving an averaged anchor.
    """
    if not spec:
        return []
    out: list[dict] = []
    for bm in bookmakers:
        bet = _bets_by_id(bm, spec["bet_id"])
        if not bet:
            continue
        values = bet.get("values", [])
        if spec["type"] == "select":
            p = _devig_select(values, spec["value"])
            raw = _raw_select(values, spec["value"])
            method = "same-book categorical de-vig"
        elif spec["type"] == "ou":
            p = _devig_ou(values, spec["side"], spec["line"])
            raw = _raw_ou(values, spec["line"])
            method = "same-book over/under de-vig"
        elif spec["type"] == "player_yes":
            p = _price_player_yes(values, spec.get("player"))
            raw = _raw_player_yes(values, spec.get("player"))
            method = "single-sided player prop haircut"
        elif spec["type"] == "player_threshold":
            p = _price_player_threshold(values, spec.get("player"),
                                        spec["side"], spec["line"])
            raw = _raw_player_threshold(values, spec.get("player"), spec["line"])
            method = "single-sided player prop haircut"
        else:
            p = None
            raw = []
            method = "unknown"
        if p is not None and 0.0 < p < 1.0:
            out.append({
                "source": "api-football",
                "bookmaker": bm.get("name") or bm.get("id") or "unknown",
                "market_key": f"af_bet_{spec['bet_id']}",
                "market_name": spec.get("label", ""),
                "contract": _contract_label(spec),
                "probability": round(p, 6),
                "probability_pct": round(p * 100, 2),
                "raw_odds": raw,
                "devig_method": method,
            })
    return out


def _raw_select(values: list[dict], target: str) -> list[dict]:
    return [
        {"name": v.get("value"), "decimal_odds": _float_or_none(v.get("odd")),
         "is_target": v.get("value", "").strip().lower() == target.strip().lower()}
        for v in values
    ]


def _raw_ou(values: list[dict], line: float) -> list[dict]:
    raw = []
    for v in values:
        ln = _parse_line(v.get("value", ""))
        if ln is not None and abs(ln - line) <= 1e-6:
            raw.append({"name": v.get("value"), "decimal_odds": _float_or_none(v.get("odd"))})
    return raw


def _raw_player_yes(values: list[dict], player: str | None) -> list[dict]:
    if not player:
        return []
    return [
        {"name": v.get("value"), "decimal_odds": _float_or_none(v.get("odd")),
         "is_target": player_matches(v.get("value", ""), player)}
        for v in values if player_matches(v.get("value", ""), player)
    ]


def _raw_player_threshold(
    values: list[dict], player: str | None, line: float
) -> list[dict]:
    if not player or line < 0 or not float(line + 0.5).is_integer():
        return []
    target = int(line + 0.5)
    raw = []
    for value in values:
        match = re.fullmatch(r"(.+?)\s*-\s*(\d+)\+", value.get("value", "").strip())
        if match and int(match.group(2)) == target and player_matches(match.group(1), player):
            raw.append({
                "name": value.get("value"),
                "decimal_odds": _float_or_none(value.get("odd")),
                "is_target": True,
            })
    return raw


def _contract_label(spec: dict) -> str:
    if spec["type"] == "select":
        return str(spec.get("value"))
    if spec["type"] == "ou":
        return f"{spec.get('side')} {spec.get('line')}"
    if spec["type"] == "player_yes":
        return f"{spec.get('player')} Yes"
    if spec["type"] == "player_threshold":
        return f"{spec.get('player')} {spec.get('side')} {spec.get('line')}"
    return ""


def _float_or_none(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
