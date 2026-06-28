"""Structured, primary-source match context for the LLM pricing layer.

The evidence bundle used to be odds-only. This module adds four deterministic,
API-Football-derived blocks the model otherwise has to scrape from betting blogs:

* ``team_form``      — each side's recent results, goals, rates, and average
                       shots/SoT/corners/cards/fouls/offsides + xG.
* ``player_form``    — per-player minutes/starts and shots/SoT/goals per 90.
* ``referee_profile``— the assigned referee's career yellows/reds per game.
* ``injuries``       — structured availability per side.

Everything is best-effort: each block is computed independently and any failure
(unsupported endpoint, referee name miss, thin sample) yields an empty block
rather than raising, so the pricing path never depends on it.
"""
from __future__ import annotations

from . import config
from .teams import player_matches

_PLAYED = {"FT", "AET", "PEN"}
_TEAM_FORM_GAMES = 6
_PLAYER_FORM_GAMES = 8
_MAX_PLAYERS_PER_TEAM = 16
_REFEREE_GAMES = 12

# settled_statistics "type" -> our short key.
_STAT_MAP = {
    "Total Shots": "shots",
    "Shots on Goal": "sot",
    "Corner Kicks": "corners",
    "Fouls": "fouls",
    "Offsides": "offsides",
    "Yellow Cards": "yellows",
    "Red Cards": "reds",
    "expected_goals": "xg",
}


def build(af, fixture, home, away, lineups) -> dict:
    """Return ``{team_form, player_form, referee_profile, injuries}``.

    Each value is empty when its data is unavailable; never raises.
    """
    fx = fixture.get("fixture") or {}
    teams = fixture.get("teams") or {}
    home_id = (teams.get("home") or {}).get("id")
    away_id = (teams.get("away") or {}).get("id")

    try:
        played = _played_fixtures(af.fixtures())
    except Exception:
        played = []

    return {
        "team_form": _safe(_team_form_both, af, played, home_id, away_id),
        "player_form": _safe(_player_form_both, af, played, home_id, away_id, lineups),
        "referee_profile": _safe(_referee_profile, af, fx.get("referee"), played),
        "injuries": _safe(_injuries_both, af, home_id, away_id),
    }


def _safe(fn, *args):
    try:
        return fn(*args) or {}
    except Exception:
        return {}


# --- shared helpers ---------------------------------------------------------

def _played_fixtures(all_fixtures) -> list[dict]:
    out = [
        fx for fx in (all_fixtures or [])
        if (((fx.get("fixture") or {}).get("status")) or {}).get("short") in _PLAYED
    ]
    out.sort(key=lambda f: (f.get("fixture") or {}).get("date") or "", reverse=True)
    return out


def _team_fixtures(played, team_id, limit) -> list[dict]:
    rows = []
    for fx in played:
        teams = fx.get("teams") or {}
        if team_id in ((teams.get("home") or {}).get("id"),
                       (teams.get("away") or {}).get("id")):
            rows.append(fx)
        if len(rows) >= limit:
            break
    return rows


def _stats(af, fixture_id) -> list[dict]:
    try:
        return af.settled_statistics(fixture_id)
    except Exception:
        return []


def _players(af, fixture_id) -> list[dict]:
    try:
        return af.fixture_players(fixture_id)
    except Exception:
        return []


def _stats_by_team(stat_response) -> dict:
    out: dict = {}
    for entry in stat_response or []:
        tid = (entry.get("team") or {}).get("id")
        if tid is None:
            continue
        vals = {}
        for item in entry.get("statistics") or []:
            key = _STAT_MAP.get(str(item.get("type")))
            num = _to_float(item.get("value")) if key else None
            if num is not None:
                vals[key] = num
        out[tid] = vals
    return out


def _to_float(value):
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).strip().replace("%", ""))
    except ValueError:
        return None


def _avg(xs) -> float:
    return round(sum(xs) / len(xs), 2) if xs else 0.0


# --- team form --------------------------------------------------------------

def _team_form_both(af, played, home_id, away_id) -> dict:
    return {
        "home": _team_form(af, played, home_id),
        "away": _team_form(af, played, away_id),
    }


def _team_form(af, played, team_id) -> dict:
    if not team_id:
        return {}
    fixtures = _team_fixtures(played, team_id, _TEAM_FORM_GAMES)
    results, gf_list, ga_list = [], [], []
    agg: dict = {k: [] for k in (
        "shots", "sot", "corners", "fouls", "offsides", "cards", "xg_for", "xg_against")}
    for fx in fixtures:
        teams = fx.get("teams") or {}
        goals = fx.get("goals") or {}
        is_home = (teams.get("home") or {}).get("id") == team_id
        gf = goals.get("home") if is_home else goals.get("away")
        ga = goals.get("away") if is_home else goals.get("home")
        if gf is None or ga is None:
            continue
        opp_side = "away" if is_home else "home"
        gf_list.append(gf)
        ga_list.append(ga)
        results.append({
            "opponent": (teams.get(opp_side) or {}).get("name"),
            "gf": gf, "ga": ga,
            "result": "W" if gf > ga else "L" if gf < ga else "D",
        })
        by_team = _stats_by_team(_stats(af, (fx.get("fixture") or {}).get("id")))
        own = by_team.get(team_id, {})
        opp = by_team.get((teams.get(opp_side) or {}).get("id"), {})
        for key in ("shots", "sot", "corners", "fouls", "offsides"):
            if key in own:
                agg[key].append(own[key])
        if "yellows" in own or "reds" in own:
            agg["cards"].append(own.get("yellows", 0) + own.get("reds", 0))
        if "xg" in own:
            agg["xg_for"].append(own["xg"])
        if "xg" in opp:
            agg["xg_against"].append(opp["xg"])
    if not results:
        return {}
    n = len(results)
    form = {
        "games": n,
        "results": results,
        "gf_avg": _avg(gf_list),
        "ga_avg": _avg(ga_list),
        "clean_sheet_rate": round(sum(1 for g in ga_list if g == 0) / n, 2),
        "btts_rate": round(sum(1 for r in results if r["gf"] > 0 and r["ga"] > 0) / n, 2),
        "over25_rate": round(sum(1 for r in results if r["gf"] + r["ga"] >= 3) / n, 2),
    }
    for label, key in (
        ("avg_shots", "shots"), ("avg_sot", "sot"), ("avg_corners", "corners"),
        ("avg_cards", "cards"), ("avg_fouls", "fouls"), ("avg_offsides", "offsides"),
        ("xg_for_avg", "xg_for"), ("xg_against_avg", "xg_against"),
    ):
        if agg[key]:
            form[label] = _avg(agg[key])
    return form


# --- player form ------------------------------------------------------------

def _player_form_both(af, played, home_id, away_id, lineups) -> dict:
    fixture_ids: list = []
    for tid in (home_id, away_id):
        for fx in _team_fixtures(played, tid, _PLAYER_FORM_GAMES):
            fid = (fx.get("fixture") or {}).get("id")
            if fid is not None and fid not in fixture_ids:
                fixture_ids.append(fid)
    per_fixture = {fid: _players(af, fid) for fid in fixture_ids}
    lineup_names = _lineup_names(lineups)
    return {
        "home": _player_form(played, per_fixture, home_id, lineup_names),
        "away": _player_form(played, per_fixture, away_id, lineup_names),
    }


def _player_form(played, per_fixture, team_id, lineup_names) -> list:
    if not team_id:
        return []
    team_fids = {(fx.get("fixture") or {}).get("id")
                 for fx in _team_fixtures(played, team_id, _PLAYER_FORM_GAMES)}
    agg: dict = {}
    for fid in team_fids:
        for entry in per_fixture.get(fid) or []:
            if (entry.get("team") or {}).get("id") != team_id:
                continue
            for pl in entry.get("players") or []:
                _accumulate_player(agg, pl)
    players = [_finalize_player(name, a) for name, a in agg.items()]
    if lineup_names:
        in_xi = [p for p in players if _name_in(p["name"], lineup_names)]
        if in_xi:
            players = in_xi
    players.sort(key=lambda p: p["minutes"], reverse=True)
    return players[:_MAX_PLAYERS_PER_TEAM]


def _accumulate_player(agg, pl) -> None:
    name = (pl.get("player") or {}).get("name")
    if not name:
        return
    stats = (pl.get("statistics") or [{}])[0] or {}
    games = stats.get("games") or {}
    minutes = _to_float(games.get("minutes")) or 0.0
    shots = stats.get("shots") or {}
    goals = stats.get("goals") or {}
    a = agg.setdefault(name, {"games": 0, "minutes": 0.0, "starts": 0,
                              "shots": 0.0, "shots_on": 0.0, "goals": 0.0})
    if minutes > 0:
        a["games"] += 1
        a["minutes"] += minutes
        if games.get("substitute") is False:
            a["starts"] += 1
    a["shots"] += _to_float(shots.get("total")) or 0.0
    a["shots_on"] += _to_float(shots.get("on")) or 0.0
    a["goals"] += _to_float(goals.get("total")) or 0.0


def _finalize_player(name, a) -> dict:
    minutes = a["minutes"]
    per90 = (lambda x: round(x / minutes * 90, 2)) if minutes > 0 else (lambda x: 0.0)
    return {
        "name": name,
        "games": a["games"],
        "starts": a["starts"],
        "minutes": int(minutes),
        "goals": int(a["goals"]),
        "shots": int(a["shots"]),
        "shots_on": int(a["shots_on"]),
        "shots_per90": per90(a["shots"]),
        "sot_per90": per90(a["shots_on"]),
        "goals_per90": per90(a["goals"]),
    }


def _lineup_names(lineups) -> list:
    names = []
    for entry in lineups or []:
        for key in ("startXI", "substitutes"):
            for pl in entry.get(key) or []:
                nm = (pl.get("player") or {}).get("name")
                if nm:
                    names.append(nm)
    return names


def _name_in(name, lineup_names) -> bool:
    return any(player_matches(name, ln) for ln in lineup_names)


# --- referee ----------------------------------------------------------------

def _referee_profile(af, referee, played) -> dict:
    """Referee discipline from this competition's played fixtures.

    API-Football's plan does not support a referee filter on /fixtures (the
    ``referee`` param is rejected), so we derive the profile from the WC fixtures
    we already cache, matched by name. The sample is therefore tournament-only and
    can be thin; the prompt tells the model to weight a low game count cautiously.
    """
    if not referee:
        return {}
    fixtures = [fx for fx in played
                if player_matches(referee, (fx.get("fixture") or {}).get("referee") or "")]
    yellows, reds, sample = [], [], []
    for fx in fixtures[:_REFEREE_GAMES]:
        by_team = _stats_by_team(_stats(af, (fx.get("fixture") or {}).get("id")))
        if not by_team:
            continue
        y = sum(v.get("yellows", 0) for v in by_team.values())
        r = sum(v.get("reds", 0) for v in by_team.values())
        yellows.append(y)
        reds.append(r)
        teams = fx.get("teams") or {}
        sample.append({
            "date": ((fx.get("fixture") or {}).get("date") or "")[:10],
            "match": f"{(teams.get('home') or {}).get('name')} vs "
                     f"{(teams.get('away') or {}).get('name')}",
            "yellows": int(y), "reds": int(r),
        })
    if not yellows:
        return {}
    return {
        "name": referee,
        "games": len(yellows),
        "yellows_per_game": _avg(yellows),
        "reds_per_game": _avg(reds),
        "note": "penalty rate is not derivable from fixture statistics; omitted.",
        "sample": sample,
    }


# --- injuries ---------------------------------------------------------------

def _injuries_both(af, home_id, away_id) -> dict:
    return {
        "home": _injuries(af, home_id),
        "away": _injuries(af, away_id),
    }


def _injuries(af, team_id) -> list:
    if not team_id:
        return []
    seen, out = set(), []
    for item in af.injuries(team_id, config.WC_SEASON) or []:
        player = item.get("player") or {}
        name = player.get("name")
        if not name or name in seen:
            continue
        seen.add(name)
        out.append({
            "player": name,
            "type": player.get("type"),
            "reason": player.get("reason"),
        })
    return out
