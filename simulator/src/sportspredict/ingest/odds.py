"""The Odds API client — the vanilla de-vig target.

Pulls 1X2 (h2h), totals and BTTS across many books (including sharp Pinnacle) and reshapes
them into the ``market_odds`` structure the engine consumes for de-vig + shrink. Free-tier
key in ``ODDS_API_KEY``. Only vanilla markets are sourced here by design; exotics are
model-only.
"""

from __future__ import annotations

import os

import requests

_BASE = "https://api.the-odds-api.com/v4"
_SPORT = "soccer_fifa_world_cup"

# The Odds API spells a few teams differently from our canonical dataset names
# (the Elo table / SportsPredict labels). ``name_match`` is loose but cannot bridge
# an abbreviation like "USA" (3 chars, no shared token) to "United States", so the
# host nation would silently fall back to pure-model. Normalize the event's spelling
# to the dataset name *for matching only* — price extraction still uses the raw name.
_API_NAME_ALIASES = {"USA": "United States"}


def _key() -> str:
    key = os.environ.get("ODDS_API_KEY")
    if not key:
        raise RuntimeError("set ODDS_API_KEY to use The Odds API")
    return key


def fetch_events(regions: str = "eu", markets: str = "h2h,totals") -> list[dict]:
    """One bulk call for all WC events. ``btts`` is NOT valid on the bulk /odds endpoint
    (422) — it needs per-event calls, too expensive on the free tier, so BTTS stays
    pure model."""
    resp = requests.get(
        f"{_BASE}/sports/{_SPORT}/odds",
        params={
            "apiKey": _key(),
            "regions": regions,
            "markets": markets,
            "oddsFormat": "decimal",
        },
        timeout=30,
    )
    if not resp.ok:  # raise without echoing the apiKey query param
        raise RuntimeError(f"odds api error {resp.status_code} for {_BASE}/sports/{_SPORT}/odds")
    return resp.json()


def to_market_odds(event: dict, home: str, away: str, bookmaker: str | None = None) -> dict:
    """Reshape one Odds-API event into engine ``market_odds`` (consensus or one book).

    Returns ``{"match_result": {"A","draw","B"}, "total_goals": {"over","under","line"},
    "btts": {"yes","no"}}`` using the median price across books (or a named bookmaker).
    """
    books = event.get("bookmakers", [])
    if bookmaker:
        books = [b for b in books if b.get("key") == bookmaker]
    by_market: dict[str, list[dict]] = {}
    for b in books:
        for m in b.get("markets", []):
            by_market.setdefault(m["key"], []).append(m)

    out: dict = {}
    if "h2h" in by_market:
        mr = _median_h2h(by_market["h2h"], home, away)
        if mr:
            out["match_result"] = mr
    if "totals" in by_market:
        tg = _median_totals(by_market["totals"])
        if tg:
            out["total_goals"] = tg
    if "btts" in by_market:
        btts = _median_two(by_market["btts"], "Yes", "No", ("yes", "no"))
        if btts:
            out["btts"] = btts
    return out


def find_event(events: list[dict], name_a: str, name_b: str) -> tuple[dict | None, bool]:
    """Find the event for two team names (loose matching). Returns (event, swapped):
    ``swapped`` is True when our team A is the event's away team."""
    from .apifootball import name_match

    for ev in events or []:
        home = _API_NAME_ALIASES.get(ev.get("home_team") or "", ev.get("home_team") or "")
        away = _API_NAME_ALIASES.get(ev.get("away_team") or "", ev.get("away_team") or "")
        if name_match(name_a, home) and name_match(name_b, away):
            return ev, False
        if name_match(name_a, away) and name_match(name_b, home):
            return ev, True
    return None, False


def market_odds_for_match(events: list[dict], name_a: str, name_b: str) -> dict | None:
    """Engine ``market_odds`` for the fixture A vs B, oriented to our team labels."""
    ev, swapped = find_event(events, name_a, name_b)
    if ev is None:
        return None
    out = to_market_odds(ev, ev.get("home_team") or "", ev.get("away_team") or "")
    if swapped and "match_result" in out:
        mr = out["match_result"]
        mr["A"], mr["B"] = mr["B"], mr["A"]
    return out or None


def _median(values: list[float]) -> float:
    s = sorted(values)
    n = len(s)
    return s[n // 2] if n % 2 else 0.5 * (s[n // 2 - 1] + s[n // 2])


def _median_h2h(markets: list[dict], home: str, away: str) -> dict:
    a, d, b = [], [], []
    for m in markets:
        price = {o["name"]: o["price"] for o in m["outcomes"]}
        if home in price and away in price:
            a.append(price[home])
            b.append(price[away])
            if "Draw" in price:
                d.append(price["Draw"])
    if not a or not b:  # no book listed both names -> no consensus, not a crash
        return {}
    res = {"A": _median(a), "B": _median(b)}
    if d:
        res["draw"] = _median(d)
    return res


def _median_totals(markets: list[dict]) -> dict:
    # Prefer the most common line (usually 2.5).
    lines: dict[float, dict] = {}
    for m in markets:
        for o in m["outcomes"]:
            point = o.get("point")
            if point is None:
                continue
            lines.setdefault(point, {"over": [], "under": []})
            side = lines[point].get(str(o.get("name", "")).lower())
            if side is not None:
                side.append(o["price"])
    lines = {p: v for p, v in lines.items() if v["over"] and v["under"]}
    if not lines:
        return {}
    line = min(lines, key=lambda p: abs(p - 2.5))
    return {
        "line": line,
        "over": _median(lines[line]["over"]),
        "under": _median(lines[line]["under"]),
    }


def _median_two(markets: list[dict], yes_name: str, no_name: str, keys: tuple) -> dict:
    yes, no = [], []
    for m in markets:
        price = {o["name"]: o["price"] for o in m["outcomes"]}
        if yes_name in price and no_name in price:
            yes.append(price[yes_name])
            no.append(price[no_name])
    if not yes or not no:
        return {}
    return {keys[0]: _median(yes), keys[1]: _median(no)}
