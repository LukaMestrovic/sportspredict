from __future__ import annotations

import hashlib
import json
import os
import time
import unicodedata
from collections import defaultdict
from pathlib import Path

import requests

from sportspredict.config import Settings, default_settings
from sportspredict.ingest import odds as _base

from .adjust import implied_mean_from_book

_BASE = "https://api.the-odds-api.com/v4"
_SPORT = "soccer_fifa_world_cup"
# Event-specific markets the lambda anchors use: team goal totals + corner/card totals.
_CORE_EVENT_MARKETS = ("btts", "team_totals", "alternate_totals_corners", "alternate_totals_cards")
_CORE_EXOTIC_MARKETS = ",".join(_CORE_EVENT_MARKETS)
_TEAM_ALIASES = {
    "usa": "united states",
    "united states of america": "united states",
    "korea republic": "south korea",
    "korea dpr": "north korea",
    "ir iran": "iran",
    "turkiye": "turkey",
    "czechia": "czech republic",
    "cote d ivoire": "ivory coast",
    "cote divoire": "ivory coast",
    "congo dr": "dr congo",
    "congo kinshasa": "dr congo",
    "cape verde islands": "cape verde",
    "cabo verde": "cape verde",
}

def _fetch_event_odds(event_id: str, regions: str, timeout: int, *, markets: str = _CORE_EXOTIC_MARKETS) -> dict:
    resp = requests.get(
        f"{_BASE}/sports/{_SPORT}/events/{event_id}/odds",
        params={"apiKey": _base._key(), "regions": regions, "markets": markets,
                "oddsFormat": "decimal"},
        timeout=timeout,
    )
    if not resp.ok:  # don't echo the apiKey query param
        raise RuntimeError(f"odds api error {resp.status_code} for event {event_id}")
    return resp.json()

# --- Live (unpaid-key) odds spend control -----------------------------------------------------
# The Odds API free tier is ~500 requests/month and each request costs (regions x markets) credits.
# The kickoff cron fires every 15 min and a match sits in the imminent window for several ticks, so
# without a cache each match's odds would be re-fetched 2-3x. This short-TTL on-disk cache reuses a
# recent response across ticks, decoupling API spend from cron frequency. Env knobs:
# SPHYBRID_ODDS_REGIONS (default "eu"), SPHYBRID_ODDS_TTL_MIN (default 20; <=0 disables),
# SPHYBRID_ODDS_LIVE_CACHE_DIR.
def live_regions(default: str = "eu") -> str:
    return os.environ.get("SPHYBRID_ODDS_REGIONS") or default

def _live_ttl_seconds() -> float:
    try:
        return float(os.environ.get("SPHYBRID_ODDS_TTL_MIN", "20")) * 60.0
    except (TypeError, ValueError):
        return 1200.0

def _live_cache_dir(settings: Settings) -> Path:
    raw = os.environ.get("SPHYBRID_ODDS_LIVE_CACHE_DIR")
    p = Path(raw) if raw else Path(settings.root) / "data" / "raw" / "odds_live_cache"
    return p if p.is_absolute() else Path(settings.root) / p

def _live_cached(path: Path, ttl: float):
    if ttl > 0 and path.exists() and (time.time() - path.stat().st_mtime) < ttl:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None
    return None

def _live_store(path: Path, payload) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        tmp.replace(path)
    except Exception:  # caching must never break a live cycle
        pass

def _live_cache_call(settings: Settings, key: str, fetch):
    """Return a cached recent response for ``key`` or call ``fetch`` and cache it."""
    ttl = _live_ttl_seconds()
    if ttl <= 0:
        return fetch()
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]
    path = _live_cache_dir(settings) / f"{digest}.json"
    hit = _live_cached(path, ttl)
    if hit is not None:
        return hit
    payload = fetch()
    _live_store(path, payload)
    return payload

# --- Country-pair matching ---------------------------------------------------------------------
def _fold_team(name: str) -> str:
    s = unicodedata.normalize("NFKD", str(name or "")).encode("ascii", "ignore").decode()
    s = "".join(ch.lower() if ch.isalnum() else " " for ch in s)
    s = " ".join(s.split())
    return _TEAM_ALIASES.get(s, s)

def _team_keys(name: str) -> set[str]:
    folded = _fold_team(name)
    keys = {folded} if folded else set()
    alias = _TEAM_ALIASES.get(folded)
    if alias:
        keys.add(alias)
    if folded == "us":
        keys.add("united states")
    return keys

def _team_match(a: str, b: str) -> bool:
    return bool(_team_keys(a) & _team_keys(b))

def find_event(events: list[dict], name_a: str, name_b: str) -> tuple[dict | None, bool]:
    """Strict country-pair matcher: only exact folded aliases (so Austria != Australia)."""
    for ev in events or []:
        home = ev.get("home_team") or ""
        away = ev.get("away_team") or ""
        if _team_match(name_a, home) and _team_match(name_b, away):
            return ev, False
        if _team_match(name_a, away) and _team_match(name_b, home):
            return ev, True
    return None, False

# --- Implied means -----------------------------------------------------------------------------
def _book_markets(event_odds: dict) -> dict[str, list[dict]]:
    by_market: dict[str, list[dict]] = defaultdict(list)
    for b in event_odds.get("bookmakers", []):
        for m in b.get("markets", []):
            by_market[m["key"]].append(m)
    return by_market

def _collect_over_under(markets: list[dict], group_of) -> dict[str, list[tuple[float, float, float]]]:
    out: dict[str, list[tuple[float, float, float]]] = defaultdict(list)
    for m in markets:
        priced: dict[tuple, float] = {}
        for o in m.get("outcomes", []):
            line = o.get("point")
            if line is None or not o.get("price"):
                continue
            priced[(group_of(o), float(line), str(o.get("name", "")).lower())] = float(o["price"])
        for (g, line, side) in list(priced):
            if side != "over":
                continue
            under = priced.get((g, line, "under"))
            if under:
                out[g].append((line, priced[(g, line, "over")], under))
    return out

def parse_exotic_means(event_odds: dict, method: str = "shin") -> dict:
    """De-vig corner/card totals and per-team goal totals into Poisson means for lambda anchoring."""
    by_market = _book_markets(event_odds)
    out: dict = {}
    for market_key, out_key in (("alternate_totals_corners", "total_corners"),
                                ("alternate_totals_cards", "total_cards")):
        groups = _collect_over_under(by_market.get(market_key, []), lambda o: "")
        mean = implied_mean_from_book(groups.get("", []), method)
        if mean is not None:
            out[out_key] = mean

    tt: dict[str, list[tuple[float, float, float]]] = defaultdict(list)
    for key in ("team_totals", "alternate_team_totals"):
        for name, rows in _collect_over_under(by_market.get(key, []), lambda o: o.get("description") or "").items():
            tt[name].extend(rows)
    team_goals = {name: implied_mean_from_book(rows, method) for name, rows in tt.items()}
    team_goals = {k: v for k, v in team_goals.items() if v is not None}
    if team_goals:
        out["team_goals"] = team_goals
    return out

# --- Live fetch + per-match anchoring inputs ----------------------------------------------------
def fetch_events_extended(
    *, settings: Settings | None = None, team_pairs: list[tuple[str, str]] | None = None,
    regions: str | None = None, timeout: int = 30, event_markets: str | None = None,
) -> list[dict]:
    settings = settings or default_settings()
    regions = regions or live_regions()  # default "eu" (one region) to conserve the free tier
    event_markets = event_markets or _CORE_EXOTIC_MARKETS
    events = _live_cache_call(
        settings, f"events|{regions}|h2h,totals",
        lambda: _base.fetch_events(regions=regions, markets="h2h,totals"),
    )
    by_id = {e.get("id"): e for e in events if e.get("id")}
    if team_pairs is None:
        targets = list(by_id)
    else:
        targets = []
        for a, b in team_pairs:
            ev, _ = find_event(events, a, b)
            if ev and ev.get("id"):
                targets.append(ev["id"])
    for eid in dict.fromkeys(targets):  # de-dup, preserve order
        try:
            event_odds = _live_cache_call(
                settings, f"event|{eid}|{regions}|{event_markets}",
                lambda: _fetch_event_odds(eid, regions, timeout, markets=event_markets),
            )
            by_id[eid]["exotic_means"] = parse_exotic_means(event_odds)
        except Exception:  # a single event's exotic fetch failing must not sink the cycle
            continue
    return list(by_id.values())

def extended_market_for_match(
    events: list[dict], name_a: str, name_b: str, *, settings: Settings | None = None,
) -> dict:
    settings = settings or default_settings()
    method = settings.markets.get("devig_method", "shin")
    ev, swapped = find_event(events, name_a, name_b)
    if ev is None:
        return {}
    out: dict = {}
    exotic = ev.get("exotic_means") or {}
    if "total_corners" in exotic:
        out["total_corners"] = exotic["total_corners"]
    if "total_cards" in exotic:
        out["total_cards"] = exotic["total_cards"]

    tg = exotic.get("team_goals") or {}
    home, away = ev.get("home_team") or "", ev.get("away_team") or ""
    home_goal = next((v for k, v in tg.items() if _team_match(k, home)), None)
    away_goal = next((v for k, v in tg.items() if _team_match(k, away)), None)
    if home_goal is not None and away_goal is not None:
        pair = [away_goal, home_goal] if swapped else [home_goal, away_goal]
        out["team_goals"] = pair

    vanilla = _base.to_market_odds(ev, home, away)
    book = vanilla.get("total_goals") or {}
    if {"line", "over", "under"} <= set(book):
        mean = implied_mean_from_book([(book["line"], book["over"], book["under"])], method)
        if mean is not None:
            out["total_goals_mean"] = mean
    return out
