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
from typing import Any

import requests

from . import cache, config
from .teams import player_matches, same_team

# Single-sided player props (only "Yes"/"Over" quoted) carry bookmaker margin we
# can't cancel against a complementary outcome; haircut the implied prob.
SINGLE_SIDE_DEVIG = 0.92


class OddsAPIRequestError(RuntimeError):
    """Secret-safe Odds API transport/status failure."""

    def __init__(self, path: str, status_code: int | None):
        self.path = path
        self.status_code = status_code
        status = str(status_code) if status_code is not None else "network"
        super().__init__(f"Odds API request failed ({status}) for {path}")


class OddsAPI:
    def __init__(self, key: str | None = None, *, refresh_odds: bool = False):
        self.key = key or config.ODDS_API_KEY
        self.refresh_odds = refresh_odds
        self._events: list[dict] | None = None
        self._odds_cache: dict[tuple[str, str], list[dict]] = {}
        self.observations: list[dict] = []

    def _get(self, path: str, **params) -> Any:
        params["apiKey"] = self.key
        try:
            r = requests.get(f"{config.ODDS_BASE}{path}", params=params, timeout=30)
            r.raise_for_status()
        except requests.RequestException as exc:
            response = getattr(exc, "response", None)
            raise OddsAPIRequestError(
                path, getattr(response, "status_code", None),
            ) from None
        return r.json()

    def events(self) -> list[dict]:
        if self._events is None:
            self._events = cache.get_or_fetch(
                "oddsapi_events", config.ODDS_SPORT,
                lambda: self._get(f"/sports/{config.ODDS_SPORT}/events"),
                ttl=3 * 3600,
                refresh=self.refresh_odds,
            )
        return self._events

    def find_event(
        self, kickoff_iso: str, home: str | None = None, away: str | None = None
    ) -> dict | None:
        target = kickoff_iso[:16]
        candidates = [e for e in self.events() if e["commence_time"][:16] == target]
        if not candidates:
            return None
        if not (home and away):
            return candidates[0] if len(candidates) == 1 else None
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
            except OddsAPIRequestError as exc:
                if exc.status_code == 422:
                    return []  # requested market bundle is unavailable
                raise

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


def observations(bookmakers: list[dict], spec: dict | None) -> list[dict]:
    """Per-book fair-probability observations for an Odds API spec."""
    if not spec:
        return []
    out: list[dict] = []
    for bm in bookmakers:
        for market in bm.get("markets", []):
            if market.get("key") != spec["market"]:
                continue
            outs = market.get("outcomes", [])
            p = _price_from_market(outs, spec)
            if p is not None and 0.0 < p < 1.0:
                out.append({
                    "source": "odds-api",
                    "bookmaker": bm.get("title") or bm.get("key") or "unknown",
                    "market_key": spec["market"],
                    "market_name": spec.get("label", spec["market"]),
                    "contract": _contract_label(spec),
                    "probability": round(p, 6),
                    "probability_pct": round(p * 100, 2),
                    "raw_odds": _raw_contract(outs, spec),
                    "devig_method": _devig_method(spec, outs),
                })
    return out


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


def _contract_label(spec: dict) -> str:
    kind = spec.get("kind")
    if kind == "multiway":
        return str(spec.get("name"))
    if kind == "yesno":
        return str(spec.get("value", "Yes"))
    if kind == "ou":
        return f"{spec.get('side')} {spec.get('line')}"
    if kind == "player_yesno":
        return f"{spec.get('player')} Yes"
    if kind == "player_ou":
        return f"{spec.get('player')} {spec.get('side')} {spec.get('line')}"
    return ""


def _raw_contract(outs: list[dict], spec: dict) -> list[dict]:
    kind = spec.get("kind")
    if kind == "multiway":
        target = str(spec.get("name", "")).lower()
        return [_raw_outcome(o, o.get("name", "").lower() == target) for o in outs]
    if kind == "yesno":
        target = str(spec.get("value", "Yes")).lower()
        return [
            _raw_outcome(o, o.get("name", "").lower() == target)
            for o in outs if o.get("name", "").lower() in ("yes", "no")
        ]
    if kind == "ou":
        line = spec.get("line")
        side = str(spec.get("side", "")).lower()
        return [
            _raw_outcome(o, o.get("name", "").lower() == side)
            for o in outs
            if abs(o.get("point", 1e9) - line) < 1e-6
            and o.get("name", "").lower() in ("over", "under")
        ]
    if kind == "player_yesno":
        player = spec.get("player")
        return [
            _raw_outcome(o, o.get("name", "").lower() == "yes")
            for o in outs
            if player_matches(o.get("description", ""), player or "")
        ]
    if kind == "player_ou":
        player = spec.get("player")
        line = spec.get("line")
        side = str(spec.get("side", "")).lower()
        return [
            _raw_outcome(o, o.get("name", "").lower() == side)
            for o in outs
            if player_matches(o.get("description", ""), player or "")
            and abs(o.get("point", 1e9) - line) < 1e-6
        ]
    return []


def _raw_outcome(outcome: dict, is_target: bool) -> dict:
    raw = {
        "name": outcome.get("name"),
        "decimal_odds": outcome.get("price"),
        "is_target": bool(is_target),
    }
    if outcome.get("point") is not None:
        raw["point"] = outcome.get("point")
    if outcome.get("description"):
        raw["description"] = outcome.get("description")
    return raw


def _devig_method(spec: dict, outs: list[dict]) -> str:
    if spec.get("kind") in ("player_yesno", "player_ou"):
        raw = _raw_contract(outs, spec)
        names = {str(o.get("name", "")).lower() for o in raw}
        if names >= {"yes", "no"} or names >= {"over", "under"}:
            return "same-book two-sided de-vig"
        return "single-sided player prop haircut"
    if spec.get("kind") == "multiway":
        return "same-book categorical de-vig"
    return "same-book two-sided de-vig"
