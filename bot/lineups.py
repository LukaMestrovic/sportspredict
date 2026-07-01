"""Lineup fetching with API-Football primary and official FIFA fallback."""
from __future__ import annotations

from .fifa import FIFA


def fetch_lineups(af, fixture: dict, *, refresh: bool = False) -> list[dict]:
    fixture_id = fixture.get("fixture", {}).get("id")
    if fixture_id:
        try:
            lineups = af.lineups(fixture_id)
            if lineups:
                return lineups
        except Exception:
            pass

    home = (fixture.get("teams", {}).get("home") or {}).get("name")
    away = (fixture.get("teams", {}).get("away") or {}).get("name")
    kickoff = (fixture.get("fixture") or {}).get("date")
    if not kickoff:
        return []
    try:
        return FIFA(refresh=refresh).lineups_for_match(kickoff, home, away)
    except Exception:
        return []
