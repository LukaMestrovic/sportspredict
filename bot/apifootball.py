"""Client for API-Football v3: fixtures (WC2026) and pre-match odds.

We map a SportPredict match to an API-Football fixture by exact kickoff
datetime, since both use the same WC2026 schedule. The fixture also gives us
the canonical home/away full team names that the question text references.
"""
from __future__ import annotations

import time

import requests

from . import cache, config
from .teams import same_team, split_match_name


class APIFootball:
    def __init__(self, key: str | None = None, *, refresh_odds: bool = False):
        self.key = key or config.APIFOOTBALL_KEY
        self.refresh_odds = refresh_odds
        self.s = requests.Session()
        self.s.headers["x-apisports-key"] = self.key
        self._fixtures_cache: list[dict] | None = None
        self._odds_cache: dict[int, list[dict]] = {}

    def _get(self, path: str, **params) -> dict:
        for attempt in range(5):
            r = self.s.get(f"{config.AF_BASE}{path}", params=params, timeout=30)
            r.raise_for_status()
            data = r.json()
            errs = data.get("errors") or {}
            if isinstance(errs, dict) and "rateLimit" in errs:  # 450/min
                time.sleep(1.5 * (attempt + 1))
                continue
            if errs:
                raise RuntimeError(f"API-Football error on {path}: {errs}")
            return data
        raise RuntimeError(f"API-Football rate limit on {path}")

    def fixtures(self) -> list[dict]:
        """All WC2026 fixtures (cached in memory + on disk, 1h TTL)."""
        if self._fixtures_cache is None:
            self._fixtures_cache = cache.get_or_fetch(
                "af_fixtures", f"{config.WC_LEAGUE_ID}-{config.WC_SEASON}",
                lambda: self._get("/fixtures", league=config.WC_LEAGUE_ID,
                                  season=config.WC_SEASON)["response"],
                ttl=3600,
            )
        return self._fixtures_cache

    def find_fixture(self, kickoff_iso: str, match_name: str | None = None) -> dict | None:
        """Match an SP match by kickoff and, when ambiguous, team identity."""
        target = kickoff_iso[:16]  # 'YYYY-MM-DDTHH:MM'
        candidates = [
            fx for fx in self.fixtures()
            if fx["fixture"]["date"][:16] == target
        ]
        if len(candidates) <= 1 or not match_name:
            return candidates[0] if candidates else None
        teams = split_match_name(match_name)
        if not teams:
            return None
        home, away = teams
        return next((
            fx for fx in candidates
            if same_team(home, fx["teams"]["home"]["name"])
            and same_team(away, fx["teams"]["away"]["name"])
        ), None)

    def odds(self, fixture_id: int) -> list[dict]:
        """Bookmaker odds blocks for a fixture (cached in memory + on disk, 6h
        TTL). Empty list if purged (settled fixtures)."""
        if fixture_id not in self._odds_cache:
            def fetch():
                resp = self._get("/odds", fixture=fixture_id)["response"]
                return resp[0]["bookmakers"] if resp else []
            self._odds_cache[fixture_id] = cache.get_or_fetch(
                "af_odds", str(fixture_id), fetch, ttl=6 * 3600,
                refresh=self.refresh_odds,
            )
        return self._odds_cache[fixture_id]

    def lineups(self, fixture_id: int) -> list[dict]:
        """Confirmed starting XI + bench per team (cached 10 min, refreshable).

        API-Football populates this ~20-40 min before kickoff; it returns an
        empty list until each side posts its sheet. Refreshed at submission
        windows like odds so a 30-minute tick sees the freshest lineup.
        """
        def fetch():
            return self._get("/fixtures/lineups", fixture=fixture_id)["response"]
        return cache.get_or_fetch(
            "af_lineups", str(fixture_id), fetch, ttl=600,
            refresh=self.refresh_odds,
        )

    def settled_statistics(self, fixture_id: int) -> list[dict]:
        """Final fixture statistics, cached forever because they are immutable."""
        return cache.get_or_fetch(
            "af_statistics", str(fixture_id),
            lambda: self._get("/fixtures/statistics", fixture=fixture_id)["response"],
            ttl=0,
        )

    def fixture_players(self, fixture_id: int) -> list[dict]:
        """Per-player stats for a (played) fixture, cached forever.

        Only meaningful once a fixture has been played, and immutable thereafter,
        so we never refresh it. Powers player form/usage aggregation.
        """
        return cache.get_or_fetch(
            "af_fixture_players", str(fixture_id),
            lambda: self._get("/fixtures/players", fixture=fixture_id)["response"],
            ttl=0,
        )

    def referee_fixtures(self, referee: str, season: int) -> list[dict]:
        """Fixtures a referee officiated in a season (cached 24h).

        Career discipline history for the assigned referee; spans all leagues, so
        it is far deeper than the handful of WC games per referee. Empty on a name
        miss, which the caller treats as 'no profile'.
        """
        return cache.get_or_fetch(
            "af_referee_fixtures", f"{referee}|{season}",
            lambda: self._get("/fixtures", referee=referee, season=season)["response"],
            ttl=24 * 3600,
        )

    def injuries(self, team_id: int, season: int) -> list[dict]:
        """Injury/suspension list for a team in a season (cached 10 min).

        Refreshed at submission windows like lineups/odds so a 30-minute tick sees
        the freshest availability.
        """
        return cache.get_or_fetch(
            "af_injuries", f"{team_id}|{season}",
            lambda: self._get("/injuries", team=team_id, season=season)["response"],
            ttl=600, refresh=self.refresh_odds,
        )
