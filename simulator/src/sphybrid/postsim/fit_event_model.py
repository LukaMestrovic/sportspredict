"""Fit compact timing/player-event priors from the already cached historical data."""

from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from sportspredict.config import default_settings


def _phase_token(elapsed, extra=0) -> tuple[str, str] | None:
    try:
        minute, added = int(elapsed or 0), int(extra or 0)
    except (TypeError, ValueError):
        return None
    if minute <= 0:
        return None
    phase = "1H" if minute <= 45 else "2H" if minute <= 90 else "ET"
    return phase, f"{minute}|{max(added, 0)}"


def _event_kinds(event: dict) -> list[str]:
    typ, detail = str(event.get("type") or "").lower(), str(event.get("detail") or "").lower()
    if "shootout" in str(event.get("comments") or "").lower():
        return []
    kinds = []
    if typ == "goal":
        if "penalty" in detail:
            kinds.append("penalties")
        if detail not in {"missed penalty"}:
            kinds.append("goals")
    if typ == "card":
        kinds.append("red_cards" if "red" in detail or "second yellow" in detail
                     else "yellow_cards")
    if typ == "subst":
        kinds.append("substitutions")
    return kinds


def _event_kind(event: dict) -> str | None:
    """Backward-compatible primary kind used by focused tests/callers."""
    kinds = _event_kinds(event)
    return kinds[-1] if kinds else None


def _smooth(hits: int, total: int) -> float:
    return (hits + 1.0) / (total + 2.0)


def fit_cached_events(cache_dir: Path, history: pd.DataFrame, players: pd.DataFrame | None) -> dict:
    stages = {str(r.match_id): str(getattr(r, "stage", "group")) for r in history.itertuples()}
    timing: dict[str, dict[str, Counter]] = defaultdict(lambda: defaultdict(Counter))
    match_totals: Counter = Counter()
    early_sub: Counter = Counter()
    scoring_goals = own_goals = 0
    seen = 0
    for path in sorted(cache_dir.glob("fixtures_events_fixture-*.json")):
        match_id = path.stem.rsplit("-", 1)[-1]
        stage = stages.get(match_id, "all")
        try:
            events = json.loads(path.read_text()).get("response") or []
        except Exception:
            continue
        seen += 1
        match_totals["all"] += 1
        match_totals[stage] += 1
        has_early_sub = False
        for event in events:
            kinds = _event_kinds(event)
            pt = _phase_token(**{
                "elapsed": (event.get("time") or {}).get("elapsed"),
                "extra": (event.get("time") or {}).get("extra"),
            })
            if not kinds or not pt:
                continue
            phase, token = pt
            for kind in kinds:
                timing[kind][phase][token] += 1
            if "goals" in kinds:
                scoring_goals += 1
                if str(event.get("detail") or "").lower() == "own goal":
                    own_goals += 1
            if "substitutions" in kinds and phase == "1H":
                has_early_sub = True
        if has_early_sub:
            early_sub["all"] += 1
            early_sub[stage] += 1

    parameters = {
        "substitute_goal_share": 0.12,
        "own_goal_share": _smooth(own_goals, scoring_goals),
    }
    if players is not None and not players.empty and {"goals", "substitute", "minutes"} <= set(players):
        played = players[players["minutes"] > 0]
        total_goals = float(played["goals"].sum())
        sub_goals = float(played.loc[played["substitute"], "goals"].sum())
        if total_goals > 0:
            parameters["substitute_goal_share"] = (sub_goals + 5.0 * 0.12) / (total_goals + 5.0)

    event_types = {
        kind: {
            phase: {"tokens": dict(sorted(tokens.items())), "n": int(sum(tokens.values()))}
            for phase, tokens in phases.items()
        }
        for kind, phases in timing.items()
    }
    rates = {k: _smooth(early_sub[k], match_totals[k]) for k in match_totals}
    result = {
        "schema_version": 1,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "n_event_matches": seen,
        "event_types": event_types,
        "binary_rates": {"substitution_before_halftime": rates},
        "parameters": parameters,
    }
    if players is not None and not players.empty and {"shots_total", "shots_on"} <= set(players):
        from .shots import fit_total_shots_model

        result["models"] = {"total_shots": fit_total_shots_model(players, history)}
    return result


def add_statsbomb_timing(data: dict, competitions: list[list[int]]) -> None:
    """Add timestamped corner/offside distributions from StatsBomb open events."""
    from statsbombpy import sb

    counters: dict[str, dict[str, Counter]] = defaultdict(lambda: defaultdict(Counter))
    for comp_id, season_id in competitions:
        matches = sb.matches(competition_id=int(comp_id), season_id=int(season_id))
        for match_id in matches["match_id"].tolist():
            try:
                events = sb.events(match_id=int(match_id))
            except Exception:
                continue
            typ = events.get("type", pd.Series(index=events.index, dtype=str)).astype(str)
            pass_type = events.get("pass_type", pd.Series(index=events.index, dtype=str)).astype(str)
            pass_outcome = events.get("pass_outcome", pd.Series(index=events.index, dtype=str)).astype(str)
            period = pd.to_numeric(events.get("period"), errors="coerce")
            minute = pd.to_numeric(events.get("minute"), errors="coerce").fillna(0).astype(int) + 1
            masks = {
                "corners": pass_type.eq("Corner"),
                # The old ingestion counted only explicit Offside rows; Pass Offside is the missing bulk.
                "offsides": typ.eq("Offside") | pass_outcome.str.contains("Offside", case=False, na=False),
            }
            for kind, mask in masks.items():
                for idx in events.index[mask]:
                    per, m = int(period.loc[idx]), int(minute.loc[idx])
                    phase = "1H" if per == 1 else "2H" if per == 2 else "ET"
                    boundary = 45 if phase == "1H" else 90 if phase == "2H" else 120
                    added = max(m - boundary, 0)
                    elapsed = boundary if added else m
                    counters[kind][phase][f"{elapsed}|{added}"] += 1
    for kind, phases in counters.items():
        data.setdefault("event_types", {})[kind] = {
            phase: {"tokens": dict(sorted(tokens.items())), "n": int(sum(tokens.values()))}
            for phase, tokens in phases.items()
        }


def reuse_statsbomb_timing(data: dict, path: Path) -> None:
    """Reuse the costly StatsBomb-only clock families while refitting API-Football events."""
    previous = json.loads(path.read_text())
    for kind in ("corners", "offsides"):
        fitted = (previous.get("event_types") or {}).get(kind)
        if fitted:
            data.setdefault("event_types", {})[kind] = fitted


def _main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Fit event_timing.json from local history caches.")
    ap.add_argument("--history", default="data/processed/history_stat_table.parquet")
    ap.add_argument("--players", default="data/processed/player_stat_table.parquet")
    ap.add_argument("--cache-dir", default="data/raw/apifootball")
    ap.add_argument("--out", default="data/processed/event_timing.json")
    ap.add_argument("--with-statsbomb", action="store_true")
    ap.add_argument(
        "--reuse-statsbomb-from", default=None,
        help="reuse corner/offside timings from an existing artifact instead of downloading events",
    )
    args = ap.parse_args(argv)
    settings = default_settings()
    history = pd.read_parquet(settings.path(args.history))
    players_path = settings.path(args.players)
    players = pd.read_parquet(players_path) if players_path.exists() else None
    data = fit_cached_events(settings.path(args.cache_dir), history, players)
    if args.with_statsbomb:
        add_statsbomb_timing(data, settings.raw["data_sources"]["statsbomb_competitions"])
    elif args.reuse_statsbomb_from:
        reuse_statsbomb_timing(data, settings.path(args.reuse_statsbomb_from))
    out = settings.path(args.out)
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    Path(out).write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    print(f"[event-model] matches={data['n_event_matches']} -> {out}")
    for kind, phases in data["event_types"].items():
        print(f"  {kind}: " + ", ".join(f"{p}={d['n']}" for p, d in phases.items()))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
