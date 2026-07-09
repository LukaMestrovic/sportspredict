"""API-Football bookmaker odds -> auditable de-vigged observations.

De-vig rule (per integration checklist): only normalize coherent outcome sets
from the SAME bookmaker and contract. We retain one fair-probability observation
per bookmaker; the Codex agent sees the spread instead of a hidden average.
"""
from __future__ import annotations

import re

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
        if str(v.get("value", "")).strip().lower() == str(target).strip().lower():
            target_imp = imp
    total = sum(implied)
    if target_imp is None or total <= 0:
        return None
    return target_imp / total


def _devig_select_sum(values: list[dict], patterns: list[str]) -> float | None:
    """Normalize a categorical set, summing outcomes whose labels match patterns."""
    implied = []
    target_imp = 0.0
    compiled = [re.compile(pattern, re.IGNORECASE) for pattern in patterns]
    for v in values:
        try:
            imp = 1.0 / float(v["odd"])
        except (ValueError, ZeroDivisionError):
            continue
        implied.append(imp)
        label = str(v.get("value", "")).strip()
        if any(pattern.search(label) for pattern in compiled):
            target_imp += imp
    total = sum(implied)
    if target_imp <= 0 or total <= 0:
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


def _api_ah_contract(side: str, line: float) -> tuple[str, str]:
    """Return API-Football's target and complement labels for a win-margin AH.

    API-Football bet 4 labels both sides with the same signed home-handicap
    row. ``Home -1.5`` pairs with ``Away -1.5``; the opposite row is
    ``Home +1.5`` / ``Away +1.5``. For an away win by N+, the target therefore
    lives on the positive home-handicap row.
    """
    opp = "Away" if side == "Home" else "Home"
    signed_line = -line if side == "Home" else line
    row = f"{signed_line:+g}"
    return f"{side} {row}", f"{opp} {row}"


def _devig_ah_pair(values: list[dict], side: str, line: float) -> float | None:
    """De-vig one API-Football Asian-Handicap row.

    Bet 4 interleaves every handicap line in one value list, so we isolate the
    single coherent two-way contract (e.g. ``Home -1.5`` vs ``Away -1.5`` for
    home "win by 2+") instead of normalizing the whole ladder like
    ``_devig_select``.
    The -line side has no push, so the pair partitions the outcome space.
    """
    target_label, other_label = _api_ah_contract(side, line)
    target = other = None
    for v in values:
        txt = v.get("value", "").strip()
        try:
            imp = 1.0 / float(v["odd"])
        except (KeyError, ValueError, ZeroDivisionError):
            continue
        if txt.lower() == target_label.lower():
            target = imp
        elif txt.lower() == other_label.lower():
            other = imp
    if target is None or other is None or (target + other) <= 0:
        return None
    return target / (target + other)


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


def observations(bookmakers: list[dict], spec: dict | None) -> list[dict]:
    """Per-book fair-probability observations for an API-Football spec.

    The bookmaker name, raw contract odds, and de-vig method are retained so the
    Codex pricing agent can audit every input without an averaged anchor.
    """
    if not spec:
        return []
    for candidate in _candidate_specs(spec):
        out = _observations_one(bookmakers, candidate)
        if out:
            return out
    return []


def _observations_one(bookmakers: list[dict], spec: dict) -> list[dict]:
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
        elif spec["type"] == "select_sum":
            p = _devig_select_sum(values, spec.get("value_patterns") or [])
            raw = _raw_select_sum(values, spec.get("value_patterns") or [])
            method = "same-book categorical sum de-vig"
        elif spec["type"] == "ou":
            p = _devig_ou(values, spec["side"], spec["line"])
            raw = _raw_ou(values, spec["line"])
            method = "same-book over/under de-vig"
        elif spec["type"] == "ah":
            p = _devig_ah_pair(values, spec["side"], spec["line"])
            raw = _raw_ah_pair(values, spec["side"], spec["line"])
            method = "same-book Asian-handicap pair de-vig"
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


def _candidate_specs(spec: dict) -> list[dict]:
    """Primary spec plus optional same-contract fallback specs."""
    candidates = [dict(spec)]
    candidates[0].pop("fallback_specs", None)
    for fallback in spec.get("fallback_specs") or []:
        item = dict(fallback)
        item.setdefault("fallback_from_bet_id", spec.get("bet_id"))
        candidates.append(item)
    return candidates


def _raw_select(values: list[dict], target: str) -> list[dict]:
    return [
        {"name": v.get("value"), "decimal_odds": _float_or_none(v.get("odd")),
         "is_target": str(v.get("value", "")).strip().lower() == str(target).strip().lower()}
        for v in values
    ]


def _raw_select_sum(values: list[dict], patterns: list[str]) -> list[dict]:
    compiled = [re.compile(pattern, re.IGNORECASE) for pattern in patterns]
    return [
        {"name": v.get("value"), "decimal_odds": _float_or_none(v.get("odd")),
         "is_target": any(pattern.search(str(v.get("value", ""))) for pattern in compiled)}
        for v in values
    ]


def _raw_ou(values: list[dict], line: float) -> list[dict]:
    raw = []
    for v in values:
        ln = _parse_line(v.get("value", ""))
        if ln is not None and abs(ln - line) <= 1e-6:
            raw.append({"name": v.get("value"), "decimal_odds": _float_or_none(v.get("odd"))})
    return raw


def _raw_ah_pair(values: list[dict], side: str, line: float) -> list[dict]:
    target, other = _api_ah_contract(side, line)
    return [
        {"name": v.get("value"), "decimal_odds": _float_or_none(v.get("odd")),
         "is_target": v.get("value", "").strip().lower() == target.lower()}
        for v in values
        if v.get("value", "").strip().lower() in (target.lower(), other.lower())
    ]


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
    if spec["type"] == "select_sum":
        return " + ".join(spec.get("value_patterns") or [])
    if spec["type"] == "ou":
        return f"{spec.get('side')} {spec.get('line')}"
    if spec["type"] == "ah":
        target, _ = _api_ah_contract(spec.get("side"), spec.get("line"))
        return target
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
