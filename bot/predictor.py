"""Predictor: bookmaker odds -> de-vigged probability for a matched market.

De-vig rule (per integration checklist): only normalize coherent outcome sets
from the SAME bookmaker and contract. We de-vig per bookmaker, then average the
fair probability across all bookmakers that quote the contract.
"""
from __future__ import annotations

import re
import unicodedata
from statistics import mean


SINGLE_SIDE_DEVIG = 0.92


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


def _norm(text: str) -> str:
    text = unicodedata.normalize("NFKD", text or "")
    return "".join(c for c in text if not unicodedata.combining(c)).lower().strip()


def _player_match(candidate: str, player: str) -> bool:
    candidate, player = _norm(candidate), _norm(player)
    if not candidate or not player:
        return False
    if candidate == player or candidate in player or player in candidate:
        return True
    surname = player.split()[-1]
    return len(surname) >= 4 and surname in candidate.split()


def _single_side_probability(odd) -> float | None:
    try:
        return min(0.99, (1.0 / float(odd)) * SINGLE_SIDE_DEVIG)
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def _price_player_yes(values: list[dict], player: str) -> float | None:
    if not player:
        return None
    value = next((v for v in values if _player_match(v.get("value", ""), player)), None)
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
        if _player_match(match.group(1), player):
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
            "label": spec.get("label", "")}
