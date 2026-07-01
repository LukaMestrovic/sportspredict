"""Public bookmaker odds snippets used as deterministic LLM evidence.

Provider APIs remain the primary odds source. This module fills narrow gaps for
public bookmaker pages that quote stat markets our APIs do not expose, keeping
the fetch cached and the parsed prices auditable.
"""
from __future__ import annotations

import html
import re
import unicodedata

import requests

from . import cache


BETOLIMP_STAT_URL = (
    "https://betolimp.co.za/sports/soccer-betting/world-cup-2026-statistics"
)
TIMEOUT = 20


def online_odds(intent: dict | None, home: str, away: str) -> list[dict]:
    """Return deterministic public online odds candidates for one market."""
    intent = intent or {}
    if _synthetic_team(home) or _synthetic_team(away):
        return []
    if _is_sot_market(intent):
        try:
            return _betolimp_sot_odds(intent, home, away)
        except Exception:
            return []
    return []


def _is_sot_market(intent: dict) -> bool:
    return intent.get("market") in {
        "team_shots_on_target",
        "total_shots_on_target",
        "shots_on_target_compare",
    }


def _betolimp_sot_odds(intent: dict, home: str, away: str) -> list[dict]:
    period = intent.get("period")
    if period not in (None, "match", "1H"):
        return []
    listing = _fetch(BETOLIMP_STAT_URL)
    event = _find_sot_event(listing, home, away)
    if not event:
        return []
    event_id, event_name = event
    url = f"{BETOLIMP_STAT_URL}/{_slug(event_name)}-{event_id}"
    lines = _text_lines(_fetch(url))
    if intent.get("market") == "team_shots_on_target":
        return _team_sot_total_candidate(intent, lines, url, event_name)
    if intent.get("market") == "total_shots_on_target":
        return _match_sot_total_candidate(intent, lines, url, event_name)
    if intent.get("market") == "shots_on_target_compare":
        return _sot_compare_candidate(intent, lines, url, event_name)
    return []


def _team_sot_total_candidate(
    intent: dict, lines: list[str], url: str, event_name: str
) -> list[dict]:
    line = _ou_line(intent)
    if line is None:
        return []
    teams = _event_teams(event_name)
    team = teams[0] if intent.get("subject") == "home" else teams[1]
    if not team:
        return []
    line_text = f"{line:g}"
    under_label = f"{team} (shots on target) ({line_text}) under"
    over_label = f"{team} (shots on target) ({line_text}) over"
    under = _decimal_after(lines, under_label)
    over = _decimal_after(lines, over_label)
    if under is None or over is None:
        return []
    side = "over" if intent.get("comparator") == "gte" else "under"
    probability = _devig_two_way(over if side == "over" else under,
                                 under if side == "over" else over)
    contract = over_label if side == "over" else under_label
    return [_candidate(
        url=url,
        contract=contract,
        quoted=f"{under_label} {under:g}; {over_label} {over:g}",
        probability=probability,
        devig_method="same-book team-stat over/under de-vig",
        why=(
            "Exact BetOlimp World Cup 2026 Statistics team shots-on-target "
            f"{contract} line for the regulation/full-time stat market."
        ),
    )]


def _match_sot_total_candidate(
    intent: dict, lines: list[str], url: str, _event_name: str
) -> list[dict]:
    line = _ou_line(intent)
    if line is None:
        return []
    line_text = f"{line:g}"
    under_label = f"Total ({line_text}) under"
    over_label = f"Total ({line_text}) over"
    under = _decimal_after(lines, under_label)
    over = _decimal_after(lines, over_label)
    if under is None or over is None:
        return []
    side = "over" if intent.get("comparator") == "gte" else "under"
    probability = _devig_two_way(over if side == "over" else under,
                                 under if side == "over" else over)
    contract = over_label if side == "over" else under_label
    return [_candidate(
        url=url,
        contract=contract,
        quoted=f"{under_label} {under:g}; {over_label} {over:g}",
        probability=probability,
        devig_method="same-book total-stat over/under de-vig",
        why=(
            "Exact BetOlimp World Cup 2026 Statistics total shots-on-target "
            f"{contract} line for the regulation/full-time stat market."
        ),
    )]


def _sot_compare_candidate(
    intent: dict, lines: list[str], url: str, event_name: str
) -> list[dict]:
    if intent.get("period") not in (None, "match"):
        return []
    teams = _event_teams(event_name)
    if not teams[0] or not teams[1]:
        return []
    home_price = _decimal_after(lines, f"{teams[0]} (shots on target)")
    draw_price = _decimal_after(lines, "Draw")
    away_price = _decimal_after(lines, f"{teams[1]} (shots on target)")
    if home_price is None or draw_price is None or away_price is None:
        return []
    side = "home" if intent.get("subject") == "home" else "away"
    selected = home_price if side == "home" else away_price
    probability = _devig_n_way(selected, [home_price, draw_price, away_price])
    team = teams[0] if side == "home" else teams[1]
    return [_candidate(
        url=url,
        contract=f"{team} (shots on target) full-time result",
        quoted=(
            f"{teams[0]} (shots on target) {home_price:g}; Draw {draw_price:g}; "
            f"{teams[1]} (shots on target) {away_price:g}"
        ),
        probability=probability,
        devig_method="same-book 3-way stat result de-vig",
        why=(
            "Exact BetOlimp World Cup 2026 Statistics full-time shots-on-target "
            f"result price for {team} to have more shots on target."
        ),
    )]


def _candidate(
    *,
    url: str,
    contract: str,
    quoted: str,
    probability: float,
    devig_method: str,
    why: str,
) -> dict:
    return {
        "source": "public-web",
        "bookmaker": "BetOlimp",
        "market_key": "betolimp_world_cup_2026_statistics",
        "url": url,
        "contract": contract,
        "quoted_price_or_odds": quoted,
        "probability_pct": round(probability * 100.0, 2),
        "devig_method": devig_method,
        "how_used": "direct online price",
        "why_relevant": why,
    }


def _ou_line(intent: dict) -> float | None:
    threshold = intent.get("threshold")
    try:
        threshold = float(threshold)
    except (TypeError, ValueError):
        return None
    if intent.get("comparator") == "gte":
        return threshold - 0.5
    if intent.get("comparator") == "lte":
        return threshold + 0.5
    return None


def _find_sot_event(text: str, home: str, away: str) -> tuple[str, str] | None:
    wanted_home = _norm_team(home)
    wanted_away = _norm_team(away)
    for event_id, event_name in _listing_events(text):
        teams = _event_teams(event_name)
        if not teams[0] or not teams[1]:
            continue
        if _norm_team(teams[0]) == wanted_home and _norm_team(teams[1]) == wanted_away:
            return event_id, event_name
    return None


def _listing_events(text: str) -> list[tuple[str, str]]:
    events = []
    for match in re.finditer(
        r'<div class="ch_line" data-id="(?P<id>\d+)">(?P<body>.*?)(?=<div class="ch_line" data-id="|\Z)',
        text,
        re.S,
    ):
        body = match.group("body")
        if "(shots on target)" not in body:
            continue
        name = re.search(r'<div class="ch_l c_name"[^>]*>(?P<name>.*?)</div>', body, re.S)
        if not name:
            continue
        clean = " ".join(_text_lines(name.group("name")))
        clean = re.sub(r"\s*#\d+\s*$", "", clean).strip()
        if "(shots on target)" in clean and " - " in clean:
            events.append((match.group("id"), clean))
    return events


def _event_teams(event_name: str) -> tuple[str | None, str | None]:
    parts = event_name.split(" - ", 1)
    if len(parts) != 2:
        return None, None
    return tuple(re.sub(r"\s*\(shots on target\)\s*", "", part).strip()
                 for part in parts)  # type: ignore[return-value]


def _decimal_after(lines: list[str], label: str) -> float | None:
    target = _norm_label(label)
    for index, line in enumerate(lines):
        if _norm_label(line) != target:
            continue
        for candidate in lines[index + 1:index + 5]:
            if re.match(r"^\d+(?:\.\d+)?$", candidate):
                return float(candidate)
    return None


def _devig_two_way(selected_price: float, other_price: float) -> float:
    selected = 1.0 / selected_price
    other = 1.0 / other_price
    return selected / (selected + other)


def _devig_n_way(selected_price: float, prices: list[float]) -> float:
    selected = 1.0 / selected_price
    total = sum(1.0 / price for price in prices)
    return selected / total


def _fetch(url: str) -> str:
    def fetch() -> str:
        response = requests.get(url, timeout=TIMEOUT, headers={"User-Agent": "Mozilla/5.0"})
        response.raise_for_status()
        return response.text

    return cache.get_or_fetch("public_odds", url, fetch, ttl=300)


def _text_lines(text: str) -> list[str]:
    text = re.sub(r"<script\b.*?</script>", "\n", text, flags=re.S | re.I)
    text = re.sub(r"<style\b.*?</style>", "\n", text, flags=re.S | re.I)
    text = html.unescape(re.sub(r"<[^>]+>", "\n", text))
    return [line.strip() for line in re.sub(r"\n+", "\n", text).splitlines()
            if line.strip()]


def _slug(text: str) -> str:
    text = _ascii(text).lower().replace("&", " and ")
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    return re.sub(r"-+", "-", text)


def _norm_team(value: str) -> str:
    value = _ascii(value).lower().replace("&", " and ")
    value = re.sub(r"\bherzegovina\b", "herzegovina", value)
    return re.sub(r"[^a-z0-9]+", "", value)


def _norm_label(value: str) -> str:
    return re.sub(r"\s+", " ", _ascii(value).lower()).strip()


def _ascii(value: str) -> str:
    return unicodedata.normalize("NFKD", str(value)).encode("ascii", "ignore").decode()


def _synthetic_team(value: str) -> bool:
    return value in {"Home", "Away", "A", "B"}
