"""The Odds API (the-odds-api.com) client — the second odds source.

Used as a FALLBACK when API-Football has no market / no bookmaker coverage.
It adds player props (anytime scorer, score-or-assist, shots-on-target, cards)
that API-Football rarely quotes for World Cup fixtures.

**Paid + metered.** Every event-odds response is cached to disk (`bot/cache.py`).
The `/events` listing is free; `/events/{id}/odds` costs
``#markets × #regions`` credits.

Outcome shapes:
  h2h / corners_1x2 / draw_no_bet : name = team name | "Draw"
  totals / *_corners / *_cards    : name = "Over"/"Under", point = line
  btts                            : name = "Yes"/"No"
  player_*                        : description = player, name = Yes/Over, point = line
"""
from __future__ import annotations

from datetime import datetime, timezone
from statistics import mean
from typing import Any

import requests

from . import cache, config
from .teams import player_matches, same_team

# Single-sided player props (only "Yes"/"Over" quoted) carry bookmaker margin we
# can't cancel against a complementary outcome; haircut the implied prob.
SINGLE_SIDE_DEVIG = 0.92


class OddsAPI:
    def __init__(self, key: str | None = None, *, refresh_odds: bool = False):
        self.key = key or config.ODDS_API_KEY
        self.refresh_odds = refresh_odds
        self._events: list[dict] | None = None
        self._odds_cache: dict[tuple[str, str], list[dict]] = {}
        self.observations: list[dict] = []

    def _get(self, path: str, **params) -> Any:
        params["apiKey"] = self.key
        r = requests.get(f"{config.ODDS_BASE}{path}", params=params, timeout=30)
        r.raise_for_status()
        return r.json()

    def events(self) -> list[dict]:
        if self._events is None:
            self._events = cache.get_or_fetch(
                "oddsapi_events", config.ODDS_SPORT,
                lambda: self._get(f"/sports/{config.ODDS_SPORT}/events"),
                ttl=3 * 3600,
            )
        return self._events

    def find_event(
        self, kickoff_iso: str, home: str | None = None, away: str | None = None
    ) -> dict | None:
        target = kickoff_iso[:16]
        candidates = [e for e in self.events() if e["commence_time"][:16] == target]
        if len(candidates) <= 1 or not (home and away):
            return candidates[0] if candidates else None
        return next((
            e for e in candidates
            if same_team(home, e["home_team"]) and same_team(away, e["away_team"])
        ), None)

    def event_odds(self, event_id: str, markets: list[str]) -> list[dict]:
        """Bookmaker blocks for the given markets (cached, one paid call)."""
        if not markets:
            return []
        mkey = ",".join(sorted(set(markets)))
        key = f"{event_id}|{mkey}|{config.ODDS_REGIONS}"
        memory_key = (event_id, mkey)
        if memory_key in self._odds_cache:
            return self._odds_cache[memory_key]

        def fetch():
            try:
                data = self._get(
                    f"/sports/{config.ODDS_SPORT}/events/{event_id}/odds",
                    regions=config.ODDS_REGIONS, markets=mkey, oddsFormat="decimal",
                )
                return data.get("bookmakers", [])
            except requests.HTTPError:
                return []  # some market bundles 422 if none available

        books = cache.get_or_fetch(
            "oddsapi_odds", key, fetch, refresh=self.refresh_odds,
        )
        self._odds_cache[memory_key] = books
        self.observations.append({
            "observed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "event_id": event_id,
            "markets": mkey.split(","),
            "bookmakers": books,
        })
        return books


# --- de-vig helpers (operate on Odds API outcome dicts) ---
def _implied(price: float) -> float | None:
    try:
        return 1.0 / float(price)
    except (ValueError, ZeroDivisionError, TypeError):
        return None


def _devig_multiway(outcomes: list[dict], target_name: str) -> float | None:
    imps, tgt = [], None
    for o in outcomes:
        imp = _implied(o["price"])
        if imp is None:
            continue
        imps.append(imp)
        if o["name"].strip().lower() == target_name.strip().lower():
            tgt = imp
    total = sum(imps)
    return tgt / total if tgt is not None and total > 0 else None


def _devig_two_sided(yes: float | None, no: float | None) -> float | None:
    yi, ni = _implied(yes) if yes else None, _implied(no) if no else None
    if yi is None:
        return None
    if ni is None:
        return min(0.99, yi * SINGLE_SIDE_DEVIG)  # single-sided haircut
    return yi / (yi + ni)


def predict(bookmakers: list[dict], spec: dict) -> dict | None:
    """spec kinds: h2h | totals | yesno | player_yesno | player_ou. Returns
    {probability, n_books, label} averaged across books, or None to skip."""
    probs: list[float] = []
    for bm in bookmakers:
        for m in bm.get("markets", []):
            if m["key"] != spec["market"]:
                continue
            outs = m["outcomes"]
            p = _price_from_market(outs, spec)
            if p is not None and 0.0 < p < 1.0:
                probs.append(p)
    if not probs:
        return None
    return {"probability": mean(probs), "n_books": len(probs),
            "book_probabilities": probs,
            "label": spec.get("label", spec["market"])}


def _price_from_market(outs: list[dict], spec: dict) -> float | None:
    kind = spec["kind"]
    if kind == "multiway":                      # h2h, corners_1x2, draw_no_bet
        return _devig_multiway(outs, spec["name"])
    if kind == "yesno":                          # btts
        y = next((o["price"] for o in outs if o["name"].lower() == "yes"), None)
        n = next((o["price"] for o in outs if o["name"].lower() == "no"), None)
        p = _devig_two_sided(y, n)
        return p if spec.get("value", "Yes") == "Yes" else (1 - p if p else None)
    if kind == "ou":                             # totals / corners / cards
        line = spec["line"]
        ov = next((o["price"] for o in outs
                   if o["name"].lower() == "over" and abs(o.get("point", 1e9) - line) < 1e-6), None)
        un = next((o["price"] for o in outs
                   if o["name"].lower() == "under" and abs(o.get("point", 1e9) - line) < 1e-6), None)
        p = _devig_two_sided(ov, un)
        return p if spec["side"] == "Over" else (1 - p if p else None)
    if kind == "player_yesno":                   # scorer, score-or-assist, card
        pl = spec["player"]
        y = next((o["price"] for o in outs
                  if player_matches(o.get("description", ""), pl) and o["name"].lower() == "yes"), None)
        n = next((o["price"] for o in outs
                  if player_matches(o.get("description", ""), pl) and o["name"].lower() == "no"), None)
        return _devig_two_sided(y, n)
    if kind == "player_ou":                      # player shots on target
        pl = spec["player"]
        line = spec["line"]
        ov = next((o["price"] for o in outs
                   if player_matches(o.get("description", ""), pl) and o["name"].lower() == "over"
                   and abs(o.get("point", 1e9) - line) < 1e-6), None)
        un = next((o["price"] for o in outs
                   if player_matches(o.get("description", ""), pl) and o["name"].lower() == "under"
                   and abs(o.get("point", 1e9) - line) < 1e-6), None)
        p = _devig_two_sided(ov, un)
        return p if spec["side"] == "Over" else (1 - p if p else None)
    return None
