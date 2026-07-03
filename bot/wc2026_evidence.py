"""Target-time World Cup empirical evidence from final API-Football events.

The deployed image is immutable, but ``cache/`` is bind-mounted and retained.
Each T-30 fire therefore fetches only final fixtures whose event response is not
already cached, rebuilds this small snapshot for the target kickoff, and overlays
the live WC scopes onto the simulator's immutable all-history evidence.
"""
from __future__ import annotations

import copy
import json
import re
from datetime import datetime, timezone
from pathlib import Path

from . import config


SNAPSHOT_PATH = config.ROOT / "cache" / "wc2026_empirical.json"
ARTIFACT_PATH = (
    config.ROOT / "simulator" / "data" / "processed" / "simulation_evidence.json"
)
FINAL_STATUSES = {"FT", "AET", "PEN"}
CORE_CONTRACTS = {
    "goal_window:after_second_hydration:et",
    "goal_window:after_second_hydration:reg",
    "red_card:match",
}
FIRST_HYDRATION_MINUTE = 22
SECOND_HYDRATION_MINUTE = 70


def known_contract_keys(path: Path = ARTIFACT_PATH) -> set[str]:
    """Every shipped exact contract, for the recurring post-settlement refresh."""
    try:
        return set((json.loads(path.read_text()).get("contracts") or {}).keys())
    except (OSError, json.JSONDecodeError):
        return set(CORE_CONTRACTS)


def refresh(
    af,
    target_kickoff: str,
    contract_keys: set[str] | None = None,
    *,
    path: Path = SNAPSHOT_PATH,
) -> dict:
    """Rebuild exact WC2026 rates using only final matches before ``target_kickoff``."""
    requested = set(contract_keys or ()) | CORE_CONTRACTS
    cutoff, eligible, covered, failures = collect_fixture_facts(
        af, target_kickoff, requested,
    )

    contracts = {}
    for key in sorted(requested):
        if not _supports(key):
            continue
        contracts[key] = {
            "wc2026": _scope_rate(key, eligible, covered, stage=None, cutoff=cutoff),
            "wc2026_knockout": _scope_rate(
                key, eligible, covered, stage="knockout", cutoff=cutoff,
            ),
        }

    snapshot = {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "target_kickoff": cutoff.isoformat(),
        "eligible_matches": len(eligible),
        "covered_matches": len(covered),
        "complete": not failures,
        "data_through": covered[-1]["kickoff"] if covered else None,
        "failures": failures,
        "contracts": contracts,
    }
    _write_atomic(path, snapshot)
    return snapshot


def collect_fixture_facts(
    af,
    target_kickoff: str,
    contract_keys: set[str],
) -> tuple[datetime, list[dict], list[dict], list[dict]]:
    """Collect immutable final-match facts shared by rates and simulator scoring."""
    cutoff = _parse_datetime(target_kickoff)
    eligible = [
        fixture for fixture in af.fixtures()
        if _fixture_time(fixture) < cutoff
        and _fixture_status(fixture) in FINAL_STATUSES
    ]
    eligible.sort(key=_fixture_time)
    needs_statistics = any(_needs_statistics(key) for key in contract_keys)
    needs_players = any(_needs_players(key) for key in contract_keys)
    covered = []
    failures = []
    for fixture in eligible:
        fixture_id = int(fixture["fixture"]["id"])
        events = None
        statistics = None
        players = None
        try:
            events = af.settled_events(fixture_id)
        except Exception as exc:
            failures.append({
                "fixture_id": fixture_id, "source": "events",
                "error": str(exc)[:200],
            })
        if needs_statistics:
            try:
                statistics = af.settled_statistics(fixture_id)
            except Exception as exc:
                failures.append({
                    "fixture_id": fixture_id, "source": "statistics",
                    "error": str(exc)[:200],
                })
        if needs_players:
            try:
                players = af.fixture_players(fixture_id)
            except Exception as exc:
                failures.append({
                    "fixture_id": fixture_id, "source": "players",
                    "error": str(exc)[:200],
                })
        covered.append({
            "fixture": fixture,
            "fixture_id": fixture_id,
            "kickoff": fixture["fixture"]["date"],
            "stage": _stage(fixture),
            "facts": _fixture_facts(
                fixture, events=events, statistics=statistics, players=players,
            ),
        })
    return cutoff, eligible, covered, failures


def supports_contract(key: str) -> bool:
    """Whether final API-Football data can label this exact contract."""
    return _supports(key)


def labels_for_contract(key: str, facts: dict) -> list[bool] | None:
    """Return the contract's natural observation units for one settled fixture."""
    return _labels(key, facts)


def overlay(estimates: dict[str, dict], snapshot: dict) -> dict[str, dict]:
    """Overlay current-tournament scopes without mutating static history records."""
    for estimate in estimates.values():
        dynamic = (snapshot.get("contracts") or {}).get(estimate.get("contract_key"))
        if not dynamic:
            continue
        history = copy.deepcopy(estimate.get("historical_evidence") or {})
        history.setdefault("contract_key", estimate.get("contract_key"))
        empirical = history.setdefault("empirical_rate", {})
        empirical.update(copy.deepcopy(dynamic))
        history["wc2026_refresh"] = {
            key: snapshot.get(key) for key in (
                "generated_at", "target_kickoff", "eligible_matches",
                "covered_matches", "complete", "data_through", "failures",
            )
        }
        estimate["historical_evidence"] = history
    return estimates


def _scope_rate(key: str, eligible: list[dict], covered: list[dict], *, stage, cutoff) -> dict:
    eligible_scope = [f for f in eligible if stage is None or _stage(f) == stage]
    covered_scope = [row for row in covered if stage is None or row["stage"] == stage]
    labels = []
    labeled_matches = 0
    source_complete = True
    for row in covered_scope:
        source_complete = source_complete and _source_available(key, row["facts"])
        values = _labels(key, row["facts"])
        if values is None:
            continue
        labels.extend(values)
        labeled_matches += 1
    if not labels:
        return {
            "available": False,
            "reason": "No exact historical labels before the target kickoff.",
            "eligible_matches": len(eligible_scope),
            "covered_matches": labeled_matches,
            "complete": source_complete,
            "target_kickoff": cutoff.isoformat(),
        }
    yes = sum(bool(value) for value in labels)
    return {
        "available": True,
        "basis": (
            "final API-Football event/stat/player data strictly before the target kickoff"
        ),
        "population": "all_labelable_matches",
        "yes_events": yes,
        "observations": len(labels),
        "matches": labeled_matches,
        "rate": round(yes / len(labels), 6),
        "eligible_matches": len(eligible_scope),
        "covered_matches": labeled_matches,
        "complete": source_complete,
        "data_through": covered_scope[-1]["kickoff"] if covered_scope else None,
        "target_kickoff": cutoff.isoformat(),
    }


def _supports(key: str) -> bool:
    return bool(
        key.startswith("goal_window:")
        or key.startswith("card_window:cards:")
        or key.startswith("red_card:")
        or key.startswith("both_teams_card:")
        or key.startswith("penalty_awarded:")
        or key.startswith("penalty_or_red:")
        or key.startswith("substitution_before_halftime:")
        or key.startswith("count:")
        or key.startswith("compare:")
        or key.startswith("total_goals:")
        or key.startswith("match_result:")
        or key.startswith("first_goal:")
        or key.startswith("compound:")
        or key.startswith("half_conditional:")
        or key.startswith("btts:")
        or key.startswith("btts_and_total:")
        or key.startswith("win_margin:")
        or key.startswith("clean_sheet:")
        or key.startswith("any_player_threshold:")
        or key.startswith("total_shots_threshold:")
        or key.startswith("substitute_score:")
    )


def _needs_statistics(key: str) -> bool:
    return bool(
        re.match(r"^(count|compare):(shots_on_target|shots_total|corners|fouls|offsides):", key)
        or key.startswith("total_shots_threshold:")
    )


def _needs_players(key: str) -> bool:
    return key.startswith(("any_player_threshold:", "substitute_score:"))


def _source_available(key: str, facts: dict) -> bool:
    if _needs_statistics(key):
        return bool(facts.get("statistics_available"))
    if _needs_players(key):
        return bool(facts.get("players_available"))
    return bool(facts.get("events_available"))


def _compare_value(value: float, comparator: str, threshold: float) -> bool:
    return {
        ">=": value >= threshold,
        ">": value > threshold,
        "<=": value <= threshold,
        "<": value < threshold,
    }[comparator]


def _stat_values(
    facts: dict, stat: str, half: str, scope: str, time_scope: str = "reg",
) -> list[float] | None:
    if stat == "goals":
        key = "match" if half == "full" and time_scope == "match" else half
        values = facts["team_goals"].get(key)
    elif stat == "cards":
        key = "match" if half == "full" and time_scope == "match" else half
        values = facts["team_cards"].get(key)
    elif half == "full":
        if time_scope == "reg" and facts.get("stats_include_extra_time"):
            return None
        values = facts["team_statistics"].get(stat)
    else:
        values = None
    if values is None or len(values) != 2 or any(value is None for value in values):
        return None
    values = [float(value) for value in values]
    if scope == "team":
        return values
    if scope == "each_team":
        return [min(values)]
    return [sum(values)]


def _labels(key: str, facts: dict) -> list[bool] | None:
    count = re.fullmatch(
        r"count:([^:]+):(team|match|each_team):(1H|2H|full):"
        r"(>=|>|<=|<):(\d+(?:\.\d+)?):(reg|match)",
        key,
    )
    if count:
        stat, scope, half, comparator, raw_threshold, time_scope = count.groups()
        values = _stat_values(facts, stat, half, scope, time_scope)
        if values is None:
            return None
        threshold = float(raw_threshold)
        return [_compare_value(value, comparator, threshold) for value in values]

    compare = re.fullmatch(r"compare:([^:]+):(1H|2H|full):(reg|match)", key)
    if compare:
        stat, half, time_scope = compare.groups()
        values = _stat_values(facts, stat, half, "team", time_scope)
        if values is None:
            return None
        return [values[0] > values[1], values[1] > values[0]]

    total_goals = re.fullmatch(
        r"total_goals:(1H|2H|full):(>=|>|<=|<):(\d+(?:\.\d+)?):(reg|match)",
        key,
    )
    if total_goals:
        half, comparator, raw_threshold, time_scope = total_goals.groups()
        key = "match" if half == "full" and time_scope == "match" else half
        values = facts["team_goals"].get(key)
        if values is None:
            return None
        return [_compare_value(sum(values), comparator, float(raw_threshold))]

    if key == "btts:full:reg":
        goals = facts["team_goals"].get("full")
        return [goals[0] > 0 and goals[1] > 0] if goals is not None else None
    if key == "btts_and_total:reg":
        goals = facts["team_goals"].get("full")
        return [
            goals[0] > 0 and goals[1] > 0 and sum(goals) >= 3
        ] if goals is not None else None
    if key == "half_conditional:halftime_lead":
        goals = facts["team_goals"].get("1H")
        return [goals[0] > goals[1], goals[1] > goals[0]] if goals is not None else None
    if key == "half_conditional:halftime_tied":
        goals = facts["team_goals"].get("1H")
        return [goals[0] == goals[1]] if goals is not None else None
    if key == "half_conditional:more_goals_2h":
        first = facts["team_goals"].get("1H")
        second = facts["team_goals"].get("2H")
        return [sum(second) > sum(first)] if first is not None and second is not None else None
    if key == "match_result:draw:reg":
        goals = facts["team_goals"].get("full")
        return [goals[0] == goals[1]] if goals is not None else None
    if key == "match_result:team:reg":
        goals = facts["team_goals"].get("full")
        return [goals[0] > goals[1], goals[1] > goals[0]] if goals is not None else None
    if key == "match_result:team:advance":
        winner = facts.get("winner")
        return [winner == 0, winner == 1] if winner in (0, 1) else None
    if key.startswith("first_goal:"):
        first = (
            facts.get("first_goal_match_team")
            if ":et:" in key else facts.get("first_goal_team")
        )
        if key.startswith("first_goal:2H"):
            first = facts.get("first_goal_2h_team")
        return [first == 0, first == 1] if first in (0, 1, None) else None
    if key == "compound:first_goal_and_other_team_scores_2h":
        first = facts.get("first_goal_team")
        second = facts["team_goals"].get("2H")
        if second is None:
            return None
        return [first == 0 and second[1] > 0, first == 1 and second[0] > 0]
    if key == "win_margin:reg:2":
        goals = facts["team_goals"].get("full")
        return [
            goals[0] - goals[1] >= 2, goals[1] - goals[0] >= 2,
        ] if goals is not None else None
    if key.startswith("clean_sheet:"):
        goals = facts["team_goals"].get("match" if key.endswith(":match") else "full")
        return [goals[1] == 0, goals[0] == 0] if goals is not None else None

    player_threshold = re.fullmatch(
        r"any_player_threshold:(goals|shots_on_target):(>=|>):(\d+(?:\.\d+)?):reg",
        key,
    )
    if player_threshold:
        stat, comparator, raw_threshold = player_threshold.groups()
        players = facts.get("players")
        if players is None or facts.get("stats_include_extra_time"):
            return None
        field = "goals" if stat == "goals" else "shots_on_target"
        threshold = float(raw_threshold)
        return [any(
            _compare_value(float(player.get(field) or 0), comparator, threshold)
            for player in players
        )]
    if key.startswith("substitute_score:"):
        players = facts.get("players")
        return [
            any(player.get("substitute") and (player.get("goals") or 0) >= 1 for player in players)
        ] if players is not None and not facts.get("stats_include_extra_time") else None
    shots = re.fullmatch(
        r"total_shots_threshold:shots_total:(>=|>|<=|<):(\d+(?:\.\d+)?):reg",
        key,
    )
    if shots:
        comparator, raw_threshold = shots.groups()
        if facts.get("stats_include_extra_time"):
            return None
        values = facts["team_statistics"].get("shots_total")
        if values is None or any(value is None for value in values):
            return None
        return [_compare_value(sum(values), comparator, float(raw_threshold))]

    value = _event_label(key, facts)
    return [value] if value is not None else None


def _event_label(key: str, facts: dict) -> bool | None:
    if not facts.get("events_available"):
        return None
    goals = facts["goals"]
    cards = facts["cards"]
    reds = facts["reds"]
    penalties = facts["penalties"]
    substitutions = facts["substitutions"]

    if key == "goal_window:after_second_hydration:et":
        return any(event["minute"] > SECOND_HYDRATION_MINUTE for event in goals)
    if key == "goal_window:after_second_hydration:reg":
        return any(SECOND_HYDRATION_MINUTE < event["minute"] <= 90 for event in goals)
    if key == "goal_window:before_first_hydration:reg":
        return any(event["minute"] <= FIRST_HYDRATION_MINUTE for event in goals)
    if key == "goal_window:stoppage:1H":
        return any(event["minute"] <= 45 and event["extra"] > 0 for event in goals)
    if key == "goal_window:stoppage:2H":
        return any(45 < event["minute"] <= 90 and event["extra"] > 0 for event in goals)
    if key.startswith("red_card:"):
        scoped = reds if key.endswith(":match") else [e for e in reds if e["minute"] <= 90]
        return bool(scoped)
    if key.startswith("both_teams_card:"):
        include_et = key.endswith(":match")
        teams = {
            event["team_id"] for event in cards
            if include_et or event["minute"] <= 90
        }
        teams.discard(None)
        return len(teams) >= 2
    if key.startswith("penalty_awarded:"):
        scoped = penalties if key.endswith(":match") else [e for e in penalties if e["minute"] <= 90]
        return bool(scoped)
    if key.startswith("penalty_or_red:"):
        include_et = key.endswith(":match")
        return bool([
            event for event in penalties + reds
            if include_et or event["minute"] <= 90
        ])
    if key.startswith("substitution_before_halftime:"):
        return any(event["minute"] <= 45 for event in substitutions)

    card_match = re.fullmatch(
        r"card_window:cards:(after_second_hydration|first_half):(et|reg):"
        r"(>=|>|<=|<):(\d+(?:\.\d+)?)", key,
    )
    if card_match:
        window, scope, comparator, raw_threshold = card_match.groups()
        selected = cards
        if scope == "reg":
            selected = [event for event in selected if event["minute"] <= 90]
        if window == "after_second_hydration":
            selected = [
                event for event in selected
                if event["minute"] > SECOND_HYDRATION_MINUTE
            ]
        else:
            selected = [event for event in selected if event["minute"] <= 45]
        return _compare(len(selected), comparator, float(raw_threshold))
    return None


def _fixture_facts(
    fixture: dict,
    *,
    events: list[dict] | None,
    statistics: list[dict] | None,
    players: list[dict] | None,
) -> dict:
    facts = {name: [] for name in ("goals", "cards", "reds", "penalties", "substitutions")}
    teams = fixture.get("teams") or {}
    team_ids = [
        (teams.get("home") or {}).get("id"),
        (teams.get("away") or {}).get("id"),
    ]
    for event in events or []:
        event_type = str(event.get("type") or "").lower()
        detail = str(event.get("detail") or "").lower()
        comments = str(event.get("comments") or "").lower()
        clock = event.get("time") or {}
        try:
            minute = int(clock.get("elapsed") or 0)
            extra = int(clock.get("extra") or 0)
        except (TypeError, ValueError):
            continue
        item = {
            "minute": minute,
            "extra": max(extra, 0),
            "team_id": (event.get("team") or {}).get("id"),
            "team_index": (
                team_ids.index((event.get("team") or {}).get("id"))
                if (event.get("team") or {}).get("id") in team_ids else None
            ),
            "detail": detail,
        }
        if event_type == "goal":
            if "penalty" in detail:
                facts["penalties"].append(item)
            if detail != "missed penalty" and "shootout" not in comments:
                facts["goals"].append(item)
        elif event_type == "card":
            facts["cards"].append(item)
            if "red" in detail or "second yellow" in detail:
                facts["reds"].append(item)
        elif event_type in {"subst", "substitution"}:
            facts["substitutions"].append(item)
    match_goals = list(facts["goals"])
    regulation_goals = [
        event for event in match_goals if event["minute"] <= 90
    ]
    first_half_goals = [
        event for event in regulation_goals if event["minute"] <= 45
    ]
    second_half_goals = [
        event for event in regulation_goals if event["minute"] > 45
    ]
    facts["team_goals"] = {
        "1H": [
            sum(event["team_index"] == index for event in first_half_goals)
            for index in (0, 1)
        ] if events is not None else None,
        "2H": [
            sum(event["team_index"] == index for event in second_half_goals)
            for index in (0, 1)
        ] if events is not None else None,
        "full": [
            sum(event["team_index"] == index for event in regulation_goals)
            for index in (0, 1)
        ] if events is not None else None,
        "match": [
            sum(event["team_index"] == index for event in match_goals)
            for index in (0, 1)
        ] if events is not None else None,
    }
    match_cards = list(facts["cards"])
    regulation_cards = [event for event in match_cards if event["minute"] <= 90]
    facts["team_cards"] = {
        "1H": [
            sum(event["team_index"] == index for event in regulation_cards if event["minute"] <= 45)
            for index in (0, 1)
        ] if events is not None else None,
        "2H": [
            sum(event["team_index"] == index for event in regulation_cards if event["minute"] > 45)
            for index in (0, 1)
        ] if events is not None else None,
        "full": [
            sum(event["team_index"] == index for event in regulation_cards)
            for index in (0, 1)
        ] if events is not None else None,
        "match": [
            sum(event["team_index"] == index for event in match_cards)
            for index in (0, 1)
        ] if events is not None else None,
    }
    ordered = sorted(regulation_goals, key=lambda event: (event["minute"], event["extra"]))
    match_ordered = sorted(match_goals, key=lambda event: (event["minute"], event["extra"]))
    second_ordered = sorted(second_half_goals, key=lambda event: (event["minute"], event["extra"]))
    facts["first_goal_team"] = ordered[0]["team_index"] if ordered else None
    facts["first_goal_match_team"] = match_ordered[0]["team_index"] if match_ordered else None
    facts["first_goal_2h_team"] = second_ordered[0]["team_index"] if second_ordered else None
    facts["events_available"] = events is not None
    facts["team_statistics"] = _team_statistics(statistics, team_ids)
    facts["players"] = _player_facts(players)
    facts["statistics_available"] = statistics is not None
    facts["players_available"] = players is not None
    facts["stats_include_extra_time"] = _fixture_status(fixture) in {"AET", "PEN"}
    winners = [
        bool((teams.get(side) or {}).get("winner"))
        for side in ("home", "away")
    ]
    facts["winner"] = winners.index(True) if True in winners else None
    return facts


def _team_statistics(statistics: list[dict] | None, team_ids: list) -> dict:
    names = {
        "Shots on Goal": "shots_on_target",
        "Total Shots": "shots_total",
        "Corner Kicks": "corners",
        "Fouls": "fouls",
        "Offsides": "offsides",
    }
    result = {name: [None, None] for name in names.values()}
    if statistics is None:
        return result
    for block in statistics:
        team_id = (block.get("team") or {}).get("id")
        if team_id not in team_ids:
            continue
        index = team_ids.index(team_id)
        for item in block.get("statistics") or []:
            name = names.get(item.get("type"))
            if name:
                value = item.get("value")
                result[name][index] = float(value or 0)
    return result


def _player_facts(players: list[dict] | None) -> list[dict] | None:
    if players is None:
        return None
    result = []
    for team in players:
        for row in team.get("players") or []:
            stats = (row.get("statistics") or [{}])[0]
            games = stats.get("games") or {}
            goals = stats.get("goals") or {}
            shots = stats.get("shots") or {}
            result.append({
                "substitute": bool(games.get("substitute")),
                "goals": int(goals.get("total") or 0),
                "shots_on_target": int(shots.get("on") or 0),
            })
    return result


def _compare(value: float, comparator: str, threshold: float) -> bool:
    return {
        ">=": value >= threshold,
        ">": value > threshold,
        "<=": value <= threshold,
        "<": value < threshold,
    }[comparator]


def _stage(fixture: dict) -> str:
    round_name = str((fixture.get("league") or {}).get("round") or "").lower()
    return "group" if "group" in round_name else "knockout"


def _fixture_time(fixture: dict) -> datetime:
    return _parse_datetime(fixture["fixture"]["date"])


def _fixture_status(fixture: dict) -> str:
    return str(((fixture.get("fixture") or {}).get("status") or {}).get("short") or "")


def _parse_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _write_atomic(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    temporary.replace(path)
