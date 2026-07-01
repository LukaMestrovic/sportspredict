"""Small official FIFA match-centre client used as a lineup fallback."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import requests

from . import cache, config
from .teams import same_team


class FIFA:
    def __init__(self, *, refresh: bool = False):
        self.refresh = refresh
        self.s = requests.Session()
        self.s.headers.update({
            "Accept": "application/json",
            "User-Agent": "sportspredict-llm/1.0",
        })

    def _get(self, path: str, **params) -> dict:
        r = self.s.get(f"{config.FIFA_BASE}{path}", params=params, timeout=20)
        r.raise_for_status()
        return r.json()

    def calendar(self) -> list[dict]:
        """Full official WC2026 match calendar, cached briefly."""
        def fetch():
            data = self._get(
                "/calendar/matches",
                idCompetition=config.FIFA_WC_COMPETITION_ID,
                idSeason=config.FIFA_WC_SEASON_ID,
                count=200,
                language="en",
            )
            return data.get("Results") or []

        return cache.get_or_fetch(
            "fifa_calendar",
            f"{config.FIFA_WC_COMPETITION_ID}-{config.FIFA_WC_SEASON_ID}",
            fetch,
            ttl=6 * 3600,
            refresh=self.refresh,
        )

    def find_match(
        self, kickoff_iso: str, home: str | None, away: str | None
    ) -> dict | None:
        """Match by kickoff minute and team names in the official calendar."""
        target = _minute_key(kickoff_iso)
        candidates = [
            m for m in self.calendar()
            if _minute_key(m.get("Date") or "") == target
        ]
        if not home or not away:
            return candidates[0] if len(candidates) == 1 else None
        for match in candidates:
            mh = _team_name(match.get("Home"))
            ma = _team_name(match.get("Away"))
            if same_team(home, mh) and same_team(away, ma):
                return match
        return None

    def lineups_for_match(
        self, kickoff_iso: str, home: str | None, away: str | None
    ) -> list[dict]:
        match = self.find_match(kickoff_iso, home, away)
        if not match:
            return []
        ids = (
            match.get("IdCompetition"),
            match.get("IdSeason"),
            match.get("IdStage"),
            match.get("IdMatch"),
        )
        if not all(ids):
            return []

        def fetch():
            return self._get(
                f"/live/football/{ids[0]}/{ids[1]}/{ids[2]}/{ids[3]}",
                language="en",
            )

        live = cache.get_or_fetch(
            "fifa_live_match",
            "|".join(str(x) for x in ids),
            fetch,
            ttl=600,
            refresh=self.refresh,
        )
        return _parse_lineups(live)


def _parse_lineups(live: dict[str, Any]) -> list[dict]:
    out = []
    match_id = live.get("IdMatch")
    for key in ("HomeTeam", "AwayTeam"):
        team = live.get(key) or {}
        players = team.get("Players") or []
        starters = [_player(p) for p in players if p.get("Status") == 1]
        subs = [_player(p) for p in players if p.get("Status") == 2]
        starters = [p for p in starters if p]
        subs = [p for p in subs if p]
        if len(starters) < 11:
            return []
        entry = {
            "team": {
                "id": _as_int(team.get("IdTeam")),
                "name": _team_name(team),
                "logo": team.get("PictureUrl"),
            },
            "formation": team.get("Tactics"),
            "startXI": [{"player": p} for p in starters],
            "substitutes": [{"player": p} for p in subs],
            "coach": _coach(team.get("Coaches") or []),
            "source": "fifa",
            "provider_match_id": match_id,
        }
        out.append(entry)
    return out if len(out) == 2 else []


def _player(raw: dict) -> dict | None:
    name = _localized(raw.get("PlayerName"))
    if not name:
        return None
    return {
        "id": _as_int(raw.get("IdPlayer")),
        "name": name,
        "number": raw.get("ShirtNumber"),
        "pos": _position(raw.get("Position")),
        "grid": None,
    }


def _coach(coaches: list[dict]) -> dict | None:
    coach = next((c for c in coaches if c.get("Role") == 0), None)
    if not coach:
        return None
    return {
        "id": _as_int(coach.get("IdCoach")),
        "name": _localized(coach.get("Alias")) or _localized(coach.get("Name")),
        "photo": coach.get("PictureUrl"),
    }


def _localized(value: Any) -> str | None:
    if isinstance(value, list):
        for item in value:
            if isinstance(item, dict) and item.get("Locale") == "en-GB":
                return item.get("Description")
        if value and isinstance(value[0], dict):
            return value[0].get("Description")
    if isinstance(value, str):
        return value
    return None


def _team_name(team: Any) -> str:
    if not isinstance(team, dict):
        return ""
    return (
        _localized(team.get("TeamName"))
        or team.get("ShortClubName")
        or team.get("Abbreviation")
        or ""
    )


def _position(value: Any) -> str | None:
    return {0: "G", 1: "D", 2: "M", 3: "F"}.get(value)


def _minute_key(value: str) -> str:
    if not value:
        return ""
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M")


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
