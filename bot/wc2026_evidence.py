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
FINAL_STATUSES = {"FT", "AET", "PEN"}
CORE_CONTRACTS = {
    "goal_window:after_second_hydration:et",
    "goal_window:after_second_hydration:reg",
    "red_card:match",
}


def refresh(
    af,
    target_kickoff: str,
    contract_keys: set[str] | None = None,
    *,
    path: Path = SNAPSHOT_PATH,
) -> dict:
    """Rebuild exact WC2026 rates using only final matches before ``target_kickoff``."""
    cutoff = _parse_datetime(target_kickoff)
    eligible = [
        fixture for fixture in af.fixtures()
        if _fixture_time(fixture) < cutoff
        and _fixture_status(fixture) in FINAL_STATUSES
    ]
    eligible.sort(key=_fixture_time)

    covered = []
    failures = []
    for fixture in eligible:
        fixture_id = int(fixture["fixture"]["id"])
        try:
            events = af.settled_events(fixture_id)
        except Exception as exc:
            failures.append({"fixture_id": fixture_id, "error": str(exc)[:200]})
            continue
        covered.append({
            "fixture_id": fixture_id,
            "kickoff": fixture["fixture"]["date"],
            "stage": _stage(fixture),
            "facts": _event_facts(fixture, events or []),
        })

    requested = set(contract_keys or ()) | CORE_CONTRACTS
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
        "complete": len(covered) == len(eligible),
        "data_through": covered[-1]["kickoff"] if covered else None,
        "failures": failures,
        "contracts": contracts,
    }
    _write_atomic(path, snapshot)
    return snapshot


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
    labels = [_label(key, row["facts"]) for row in covered_scope]
    labels = [value for value in labels if value is not None]
    if not labels:
        return {
            "available": False,
            "reason": "No exact historical labels before the target kickoff.",
            "eligible_matches": len(eligible_scope),
            "covered_matches": len(covered_scope),
            "complete": len(eligible_scope) == len(covered_scope),
            "target_kickoff": cutoff.isoformat(),
        }
    yes = sum(bool(value) for value in labels)
    return {
        "available": True,
        "basis": "final API-Football event data strictly before the target kickoff",
        "yes_events": yes,
        "observations": len(labels),
        "matches": len(labels),
        "rate": round(yes / len(labels), 6),
        "eligible_matches": len(eligible_scope),
        "covered_matches": len(covered_scope),
        "complete": len(eligible_scope) == len(covered_scope),
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
    )


def _label(key: str, facts: dict) -> bool | None:
    goals = facts["goals"]
    cards = facts["cards"]
    reds = facts["reds"]
    penalties = facts["penalties"]
    substitutions = facts["substitutions"]

    if key == "goal_window:after_second_hydration:et":
        return any(event["minute"] > 67 for event in goals)
    if key == "goal_window:after_second_hydration:reg":
        return any(67 < event["minute"] <= 90 for event in goals)
    if key == "goal_window:before_first_hydration:reg":
        return any(event["minute"] <= 22 for event in goals)
    if key == "goal_window:stoppage:1H":
        return any(event["minute"] <= 45 and event["extra"] > 0 for event in goals)
    if key == "goal_window:stoppage:2H":
        return any(45 < event["minute"] <= 90 and event["extra"] > 0 for event in goals)
    if key.startswith("red_card:"):
        scoped = reds if key.endswith(":match") else [e for e in reds if e["minute"] <= 90]
        return bool(scoped)
    if key.startswith("both_teams_card:"):
        teams = {event["team_id"] for event in cards if event["minute"] <= 90}
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
            selected = [event for event in selected if event["minute"] > 67]
        else:
            selected = [event for event in selected if event["minute"] <= 45]
        return _compare(len(selected), comparator, float(raw_threshold))
    return None


def _event_facts(fixture: dict, events: list[dict]) -> dict:
    facts = {name: [] for name in ("goals", "cards", "reds", "penalties", "substitutions")}
    for event in events:
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
    return facts


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
