"""API-Football (api-sports.io) client — the one paid source.

Fills the gaps free data cannot: 2026 fixtures, confirmed lineups, the referee per fixture,
and first/second-half statistic splits (plus pre-match odds). Responses are cached to disk to
respect the request quota. Requires ``APIFOOTBALL_KEY`` in the environment.
"""

from __future__ import annotations

import json
import os
import re
from datetime import date
from pathlib import Path

import requests

from ..features.context import PlayerInfo

_BASE = "https://v3.football.api-sports.io"
_CACHE = Path("data/raw/apifootball")
_POS = {"G": "GK", "D": "DF", "M": "MF", "F": "FW"}


def _key() -> str:
    key = os.environ.get("APIFOOTBALL_KEY")
    if not key:
        raise RuntimeError("set APIFOOTBALL_KEY to use API-Football")
    return key


def _get(endpoint: str, params: dict, use_cache: bool = True) -> dict:
    _CACHE.mkdir(parents=True, exist_ok=True)
    cache_file = _CACHE / (endpoint.replace("/", "_") + "_" + _slug(params) + ".json")
    if use_cache and cache_file.exists():
        return json.loads(cache_file.read_text())
    resp = requests.get(
        f"{_BASE}/{endpoint}",
        params=params,
        headers={"x-apisports-key": _key()},
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    cache_file.write_text(json.dumps(data))
    return data


def _slug(params: dict) -> str:
    return "_".join(f"{k}-{v}" for k, v in sorted(params.items()))


def fixtures(league: int, season: int) -> list[dict]:
    """All fixtures for a competition/season (e.g. World Cup 2026)."""
    return _get("fixtures", {"league": league, "season": season})["response"]


def lineups(fixture_id: int) -> list[dict]:
    return _get("fixtures/lineups", {"fixture": fixture_id})["response"]


def fixture_statistics(fixture_id: int, half: bool = True) -> list[dict]:
    """Fixture statistics; ``half=True`` requests first/second-half splits where available."""
    params = {"fixture": fixture_id}
    if half:
        params["half"] = "true"
    return _get("fixtures/statistics", params)["response"]


def referee_for_fixture(fixture: dict) -> str | None:
    return (fixture.get("fixture", {}) or {}).get("referee")


def fixture_events(fixture_id: int) -> list[dict]:
    """Match events (goals, cards, VAR, ...) — used for penalties awarded."""
    return _get("fixtures/events", {"fixture": fixture_id})["response"]


def injuries(fixture_id: int) -> list[dict]:
    return _get("injuries", {"fixture": fixture_id})["response"]


def squad(team_id: int) -> list[dict]:
    """Current squad list for a team (cached; one call per team for the tournament)."""
    return _get("players/squads", {"team": team_id})["response"]


# -- pure transforms (unit-tested without a network call) -------------------
def parse_lineup(response: list[dict], sub_start_prob: float = 0.35) -> dict[str, list[PlayerInfo]]:
    """Lineups response -> ``{team_name: [PlayerInfo,...]}`` (starters + subs).

    ``sub_start_prob`` weights bench exposure (config: ``players.sub_start_prob``).
    """
    out: dict[str, list[PlayerInfo]] = {}
    for entry in response or []:
        team = (entry.get("team") or {}).get("name")
        if not team:
            continue
        players: list[PlayerInfo] = []
        for slot, start_prob in (("startXI", 1.0), ("substitutes", sub_start_prob)):
            for item in entry.get(slot) or []:
                pl = item.get("player") or {}
                name = pl.get("name")
                if not name:
                    continue
                pos = _POS.get((pl.get("pos") or "")[:1].upper(), "MF")
                players.append(PlayerInfo(name=name, team=team, position=pos, start_prob=start_prob))
        out[team] = players
    return out


def parse_discipline(stats_response: list[dict]) -> dict[str, float]:
    """Statistics response -> match totals {fouls, yellows, reds} summed over both teams.

    Returns ``{}`` when the response carries none of those statistics (e.g. data not
    recorded for the fixture), so callers can skip it rather than log zeros.
    """
    wanted = {"Fouls": "fouls", "Yellow Cards": "yellows", "Red Cards": "reds"}
    out = {"fouls": 0.0, "yellows": 0.0, "reds": 0.0}
    seen = False
    for entry in stats_response or []:
        for item in entry.get("statistics") or []:
            key = wanted.get(str(item.get("type")))
            if key:
                seen = True
                val = item.get("value")
                out[key] += float(val) if val is not None else 0.0
    return out if seen else {}


def parse_penalties(events_response: list[dict]) -> int:
    """Events response -> penalty kicks awarded in play (shootout kicks excluded)."""
    n = 0
    for ev in events_response or []:
        etype = str(ev.get("type") or "").lower()
        detail = str(ev.get("detail") or "").lower()
        comments = str(ev.get("comments") or "").lower()
        if "shootout" in detail or "shootout" in comments:
            continue
        if etype == "goal" and detail in ("penalty", "missed penalty"):
            n += 1
    return n


_SQUAD_POS = {"Goalkeeper": "GK", "Defender": "DF", "Midfielder": "MF", "Attacker": "FW"}


def parse_squad(response: list[dict]) -> list[PlayerInfo]:
    """Squad response -> [PlayerInfo] with positions. Used for player-market team
    attribution and position priors when lineups are not yet published; players absent
    from both squads are skipped by the parser instead of priced."""
    out: list[PlayerInfo] = []
    for entry in response or []:
        team = (entry.get("team") or {}).get("name") or ""
        for pl in entry.get("players") or []:
            name = pl.get("name")
            if not name:
                continue
            pos = _SQUAD_POS.get(str(pl.get("position")), "MF")
            out.append(PlayerInfo(name=name, team=team, position=pos))
    return out


def parse_injuries(response: list[dict]) -> list[tuple[str, str]]:
    """Injuries response -> ``[(team_name, player_name)]`` for players ruled out."""
    out: list[tuple[str, str]] = []
    for entry in response or []:
        pl = entry.get("player") or {}
        team = (entry.get("team") or {}).get("name")
        name = pl.get("name")
        if name and team and "missing" in str(pl.get("type", "")).lower():
            out.append((team, name))
    return out


# Generic tokens that must not by themselves make two country names "match".
_NAME_STOP = {
    "republic", "united", "states", "north", "south", "new", "saudi", "of", "and",
    "dr", "pr", "the", "island", "islands", "coast", "herzegovina", "arab", "emirates",
    "democratic", "people", "rep", "fc",
}


def _fold(s: str) -> str:
    """Accent-fold to ASCII so "Türkiye" matches "Turkiye"/"Turkey"."""
    import unicodedata

    return unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()


def _tokens(s: str) -> set[str]:
    toks = set(re.sub(r"[^a-z ]", " ", _fold(s)).split())
    return (toks - _NAME_STOP) or toks  # keep originals only if stripping empties it


def name_match(a: str, b: str) -> bool:
    """Loose team-name match across naming conventions (token overlap or 4+ char prefix)."""
    if a and b and _fold(a).strip() == _fold(b).strip():
        return True  # exact (incl. 3-letter codes, which the token rules ignore)
    ta, tb = _tokens(a), _tokens(b)
    if ta & tb:
        return True
    for w1 in ta:
        for w2 in tb:
            if len(w1) >= 4 and len(w2) >= 4 and (w1.startswith(w2[:4]) or w2.startswith(w1[:4])):
                return True
    return False


def _day_diff(d1: str, d2: str) -> int:
    try:
        return abs((date.fromisoformat(d1) - date.fromisoformat(d2)).days)
    except ValueError:
        return 99


def find_fixture(fixtures: list[dict], name_a: str, name_b: str, when: str | None) -> dict | None:
    """Find the fixture matching two teams (and ~date). Best-effort; None if unconfident."""
    target = (when or "")[:10]
    best, best_score = None, 0
    for fx in fixtures or []:
        fdate = ((fx.get("fixture") or {}).get("date") or "")[:10]
        if target and fdate and _day_diff(fdate, target) > 1:
            continue
        teams = fx.get("teams") or {}
        home = ((teams.get("home") or {}).get("name") or "")
        away = ((teams.get("away") or {}).get("name") or "")
        direct = name_match(name_a, home) + name_match(name_b, away)
        swapped = name_match(name_a, away) + name_match(name_b, home)
        score = max(direct, swapped)
        if score > best_score:
            best, best_score = fx, score
    return best if best_score >= 2 else None  # require both teams to match
