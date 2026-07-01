"""Leakage-controlled rolling-origin backtest for the LLM-facing exotic markets.

Each test year is genuinely unseen: the rate GBMs, team ratings, point-in-time Elo, event clocks,
substitution base rate and player shares use only matches from earlier dates.  Labels come from the
historical count table, cached API-Football events, StatsBomb events and the player match table.
"""

from __future__ import annotations

import argparse
import contextlib
import copy
import gc
import json
import tempfile
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd

from sportspredict.config import Settings, default_settings
from sportspredict.features.context import MatchContext
from sportspredict.types import H2, SHOTS_ON_TARGET, TEAM_A, TEAM_B, YELLOWS

from ..engine import build_engine
from ..rates.assemble import results_from_stat_table
from ..rates.team_ratings import fit_team_ratings
from ..rates.train import train_rate_models
from .fit_shares import fit_shares, write_shares
from .contracts import contract_key
from .markets import FIRST_HYDRATION_MINUTE, SECOND_HYDRATION_MINUTE

EVENT_TABLE = "data/processed/exotic_event_table.parquet"
HISTORY_TABLE = "data/processed/history_stat_table.parquet"
PLAYER_TABLE = "data/processed/player_stat_table.parquet"
DEFAULT_TEST_YEARS = (2021, 2022, 2023, 2024, 2025, 2026)

MARKET_NAMES = {
    "penalty_awarded": "Penalty awarded",
    "penalty_or_red": "Penalty or red card",
    "both_teams_sot_1h": "Both teams 1+ SoT — 1H",
    "both_teams_sot_2h": "Both teams 1+ SoT — 2H",
    "total_sot_2h_4plus": "Total 4+ SoT — 2H",
    "team_more_sot_2h": "Team has more SoT — 2H",
    "team_sot_2h_2plus": "Team 2+ SoT — 2H",
    "team_card_2h_1plus": "Team 1+ card — 2H",
    "first_goal": "Team scores first",
    "first_goal_2h": "Team scores first — 2H",
    "first_goal_and_other_scores_2h": "First goal AND other team scores — 2H",
    "goal_before_first_hydration": "Goal before first hydration break",
    "goal_after_second_hydration_reg": "Goal after second hydration break — regulation",
    "goal_h1_stoppage": "Goal in 1H stoppage time",
    "goal_h2_stoppage": "Goal in 2H stoppage time",
    "card_after_second_hydration": "Card after second hydration break",
    "offside_before_first_hydration": "Offside before first hydration break",
    "corners_before_first_hydration_2plus": "2+ corners before first hydration break",
    "substitution_before_halftime": "Substitution before halftime",
    "substitute_scores": "A substitute scores",
    "any_player_sot_2plus": "Any player 2+ SoT",
    "any_player_brace": "Any player scores 2+ goals",
    "red_card": "A red card is shown",
    "card_first_half": "A card is shown — 1H",
    "both_teams_card": "Both teams receive a card — regulation",
    "win_margin_2plus": "Team wins by 2+ goals — regulation",
    "total_shots_full_20plus": "Total 20+ shots — regulation",
    "total_shots_full_22plus": "Total 22+ shots — regulation",
    "total_sot_full_8plus": "Total 8+ SoT — regulation",
    "halftime_lead": "Team leads at halftime",
    "more_goals_2h": "Second half has more goals",
    "btts_and_total_3plus": "BTTS and 3+ goals — regulation",
}
for _threshold in range(2, 9):
    MARKET_NAMES[f"team_sot_full_{_threshold}plus"] = f"Team {_threshold}+ SoT — full match"


def _phase(elapsed: int) -> str | None:
    if elapsed <= 0:
        return None
    return "1H" if elapsed <= 45 else "2H" if elapsed <= 90 else "ET"


def _order_value(phase: str, minute: float, extra: float, sequence: int = 0) -> float:
    if phase == "1H" and extra > 0:
        base = 45.0 + min(extra, 99.0) / 100.0
    elif phase == "2H" and extra > 0:
        base = 90.0 + min(extra, 99.0) / 100.0
    elif phase == "ET":
        base = 100.0 + minute + min(extra, 99.0) / 100.0
    else:
        base = minute
    return base + sequence * 1e-7


def _api_event_rows(history: pd.DataFrame, settings: Settings) -> list[dict]:
    from ..rates.ingest_apifootball import Canonicalizer
    from sportspredict.ingest.elo import load_elo_table

    try:
        elo = load_elo_table(settings.path("data/raw/elo.csv"))
    except Exception:
        elo = {}
    canon = Canonicalizer(elo)
    cache = settings.path("data/raw/apifootball")
    rows: list[dict] = []
    api = history[history["source"] == "apifootball"]
    for match in api.itertuples(index=False):
        path = cache / f"fixtures_events_fixture-{int(match.match_id)}.json"
        if not path.exists():
            continue
        try:
            events = json.loads(path.read_text()).get("response") or []
        except Exception:
            continue
        key = {"match_id": int(match.match_id), "source": "apifootball"}
        rows.append({**key, "event_type": "__match__", "team_side": None,
                     "phase": None, "minute": 0, "extra": 0, "sequence": -1})
        for sequence, event in enumerate(events):
            clock = event.get("time") or {}
            try:
                minute = int(clock.get("elapsed") or 0)
                extra = int(clock.get("extra") or 0)
            except (TypeError, ValueError):
                continue
            phase = _phase(minute)
            if phase is None:
                continue
            typ = str(event.get("type") or "").lower()
            detail = str(event.get("detail") or "").lower()
            team = canon((event.get("team") or {}).get("name") or "")
            side = "home" if team == match.home_team else "away" if team == match.away_team else None

            def emit(event_type: str, event_side=side):
                rows.append({**key, "event_type": event_type, "team_side": event_side,
                             "phase": phase, "minute": minute, "extra": max(extra, 0),
                             "sequence": sequence})

            if typ == "goal":
                if "shootout" in str(event.get("comments") or "").lower():
                    continue
                if "penalty" in detail:
                    emit("penalties")
                if detail == "missed penalty":
                    continue
                scorer_side = side
                if detail == "own goal" and side in {"home", "away"}:
                    scorer_side = "away" if side == "home" else "home"
                emit("goals", scorer_side)
                if detail == "own goal":
                    emit("own_goals", scorer_side)
            elif typ == "card":
                emit("red_cards" if "red" in detail or "second yellow" in detail
                     else "yellow_cards")
            elif typ == "subst":
                emit("substitutions")
    return rows


def _statsbomb_event_rows(history: pd.DataFrame, cache_dir: Path) -> list[dict]:
    """Fetch raw open-data JSON one match at a time and checkpoint the compact derived rows."""
    from urllib.request import urlopen

    cache_dir.mkdir(parents=True, exist_ok=True)
    rows: list[dict] = []
    sb_matches = history[history["source"] == "statsbomb"]
    for match in sb_matches.itertuples(index=False):
        cached = cache_dir / f"{int(match.match_id)}.json"
        if cached.exists():
            try:
                rows.extend(json.loads(cached.read_text()))
                continue
            except Exception:
                cached.unlink(missing_ok=True)
        url = ("https://raw.githubusercontent.com/statsbomb/open-data/master/data/events/"
               f"{int(match.match_id)}.json")
        try:
            with urlopen(url, timeout=30) as response:
                events = json.load(response)
        except Exception:
            continue
        key = {"match_id": int(match.match_id), "source": "statsbomb"}
        match_rows = [{**key, "event_type": "__match__", "team_side": None,
                       "phase": None, "minute": 0, "extra": 0, "sequence": -1}]
        for sequence, event in enumerate(events):
            typ = str((event.get("type") or {}).get("name") or "")
            pass_data = event.get("pass") or {}
            pass_type = str((pass_data.get("type") or {}).get("name") or "")
            pass_outcome = str((pass_data.get("outcome") or {}).get("name") or "")
            event_types = []
            if pass_type == "Corner":
                event_types.append("corners")
            if typ == "Offside" or "Offside" in pass_outcome:
                event_types.append("offsides")
            if not event_types:
                continue
            try:
                period, raw_minute = int(event.get("period")), int(event.get("minute", 0)) + 1
            except (TypeError, ValueError):
                continue
            phase = "1H" if period == 1 else "2H" if period == 2 else "ET"
            boundary = 45 if phase == "1H" else 90 if phase == "2H" else 120
            extra = max(raw_minute - boundary, 0)
            elapsed = boundary if extra else raw_minute
            for event_type in event_types:
                match_rows.append({**key, "event_type": event_type, "team_side": None,
                                   "phase": phase, "minute": elapsed, "extra": extra,
                                   "sequence": sequence})
        cached.write_text(json.dumps(match_rows, separators=(",", ":")))
        rows.extend(match_rows)
    return rows


def build_event_table(
    settings: Settings | None = None, *, out_path: str | Path | None = None,
    include_statsbomb: bool = True,
) -> pd.DataFrame:
    """Consolidate local/API and StatsBomb events into the compact backtest event table."""
    settings = settings or default_settings()
    history = pd.read_parquet(settings.path(HISTORY_TABLE))
    rows = _api_event_rows(history, settings)
    if include_statsbomb:
        rows.extend(_statsbomb_event_rows(
            history, settings.path("data/raw/statsbomb_exotic_events")
        ))
    table = pd.DataFrame.from_records(rows)
    if not table.empty:
        table = table.astype({"match_id": "int64", "source": "string",
                              "event_type": "string", "team_side": "string",
                              "phase": "string", "minute": "int16", "extra": "int16",
                              "sequence": "int32"})
    target = settings.path(out_path or EVENT_TABLE)
    target.parent.mkdir(parents=True, exist_ok=True)
    table.to_parquet(target, index=False)
    return table


def point_in_time_elos(history: pd.DataFrame, *, k: float = 20.0) -> pd.DataFrame:
    """Pre-match Elo using only earlier dates; same-date matches share the same snapshot."""
    frame = history.copy()
    frame["_date"] = pd.to_datetime(frame["match_date"], errors="coerce")
    frame = frame.sort_values(["_date", "source", "match_id"])
    ratings: dict[str, float] = {}
    home_pre, away_pre = {}, {}
    for match_date, group in frame.groupby("_date", sort=True):
        del match_date
        updates: list[tuple[str, str, float]] = []
        for idx, row in group.iterrows():
            home, away = str(row.home_team), str(row.away_team)
            rh, ra = ratings.get(home, 1500.0), ratings.get(away, 1500.0)
            home_pre[idx], away_pre[idx] = rh, ra
            hg = float(row.home_goals_h1 + row.home_goals_h2)
            ag = float(row.away_goals_h1 + row.away_goals_h2)
            score = 1.0 if hg > ag else 0.0 if hg < ag else 0.5
            expected = 1.0 / (1.0 + 10.0 ** ((ra - rh) / 400.0))
            updates.append((home, away, k * (score - expected)))
        for home, away, delta in updates:
            ratings[home] = ratings.get(home, 1500.0) + delta
            ratings[away] = ratings.get(away, 1500.0) - delta
    out = history.copy()
    out["home_elo"] = pd.Series(home_pre).reindex(history.index).fillna(1500.0)
    out["away_elo"] = pd.Series(away_pre).reindex(history.index).fillna(1500.0)
    return out


def _generated_contract_key(market: str, stage: str) -> str:
    """Contract key for one deterministic historical-label template."""
    if market.startswith("team_sot_full_"):
        threshold = int(market.removeprefix("team_sot_full_").removesuffix("plus"))
        return contract_key("count_threshold", {
            "stat": "shots_on_target", "scope": "team", "half": "full",
            "comparator": ">=", "threshold": threshold, "regulation": True,
        }, stage=stage)
    mapping = {
        "both_teams_sot_1h": ("count_threshold", {"stat": "shots_on_target", "scope": "each_team", "half": "1H", "comparator": ">=", "threshold": 1}),
        "both_teams_sot_2h": ("count_threshold", {"stat": "shots_on_target", "scope": "each_team", "half": "2H", "comparator": ">=", "threshold": 1}),
        "total_sot_2h_4plus": ("count_threshold", {"stat": "shots_on_target", "scope": "match", "half": "2H", "comparator": ">=", "threshold": 4}),
        "team_more_sot_2h": ("team_vs_team_more", {"stat": "shots_on_target", "half": "2H"}),
        "team_sot_2h_2plus": ("count_threshold", {"stat": "shots_on_target", "scope": "team", "half": "2H", "comparator": ">=", "threshold": 2}),
        "team_card_2h_1plus": ("count_threshold", {"stat": "cards", "scope": "team", "half": "2H", "comparator": ">=", "threshold": 1}),
        "total_sot_full_8plus": ("count_threshold", {"stat": "shots_on_target", "scope": "match", "half": "full", "comparator": ">=", "threshold": 8, "regulation": True}),
        "halftime_lead": ("half_conditional", {"subtype": "halftime_lead"}),
        "more_goals_2h": ("half_conditional", {"subtype": "more_goals_2h"}),
        "btts_and_total_3plus": ("btts_and_total", {"regulation": True}),
        "win_margin_2plus": ("win_margin", {"threshold": 2, "regulation": True}),
        "first_goal": ("first_goal", {"half": None}),
        "first_goal_2h": ("first_goal", {"half": "2H"}),
        "first_goal_and_other_scores_2h": ("compound_and", {}),
        "goal_before_first_hydration": ("goal_window", {"window": "before_first_hydration"}),
        "goal_after_second_hydration_reg": ("goal_window", {"window": "after_second_hydration", "include_et": False}),
        "goal_h1_stoppage": ("goal_window", {"window": "stoppage", "half": "1H"}),
        "goal_h2_stoppage": ("goal_window", {"window": "stoppage", "half": "2H"}),
        "card_after_second_hydration": ("card_window", {"window": "after_second_hydration", "include_et": True}),
        "red_card": ("red_card", {}),
        "card_first_half": ("card_window", {"window": "first_half"}),
        "both_teams_card": ("both_teams_card", {"regulation": True}),
        "substitution_before_halftime": ("substitution_before_halftime", {}),
        "penalty_awarded": ("penalty_awarded", {"regulation": True}),
        "penalty_or_red": ("penalty_or_red", {"regulation": True}),
        "offside_before_first_hydration": ("stat_window", {"stat": "offsides", "window": "before_first_hydration", "comparator": ">=", "threshold": 1}),
        "corners_before_first_hydration_2plus": ("stat_window", {"stat": "corners", "window": "before_first_hydration", "comparator": ">=", "threshold": 2}),
        "any_player_sot_2plus": ("any_player_threshold", {"stat": "shots_on_target", "comparator": ">=", "threshold": 2}),
        "any_player_brace": ("any_player_threshold", {"stat": "goals", "comparator": ">", "threshold": 1}),
        "substitute_scores": ("substitute_score", {"regulation": True}),
        "total_shots_full_20plus": ("total_shots_threshold", {"stat": "shots_total", "comparator": ">=", "threshold": 20}),
        "total_shots_full_22plus": ("total_shots_threshold", {"stat": "shots_total", "comparator": ">=", "threshold": 22}),
    }
    family, params = mapping[market]
    return contract_key(family, params, stage=stage)


def _add_case(records: list[dict], row, market: str, question: str, outcome: bool) -> None:
    records.append({
        "match_id": int(row.match_id), "source": str(row.source),
        "match_date": str(row.match_date), "year": int(pd.Timestamp(row.match_date).year),
        "home_team": str(row.home_team), "away_team": str(row.away_team),
        "tournament": str(row.tournament), "stage": str(row.stage),
        "market": market, "market_name": MARKET_NAMES[market], "question": question,
        "contract_key": _generated_contract_key(market, str(row.stage)),
        "outcome": int(bool(outcome)),
    })


def build_question_table(
    history: pd.DataFrame, events: pd.DataFrame, players: pd.DataFrame,
) -> pd.DataFrame:
    """Build every historically labelable old/new no-direct question."""
    records: list[dict] = []
    history = history[pd.to_datetime(history["match_date"], errors="coerce").notna()].copy()

    # Count-derived old exotic templates: labels exist for every historical match.
    for row in history.itertuples(index=False):
        a, b = str(row.home_team), str(row.away_team)
        ah1, bh1 = float(row.home_shots_on_target_h1), float(row.away_shots_on_target_h1)
        ah2, bh2 = float(row.home_shots_on_target_h2), float(row.away_shots_on_target_h2)
        # API-Football supplies only full-match team-stat totals (stored in H1 with H2=0 by the
        # ingestion shim), so half-SoT labels are trustworthy only on the StatsBomb rows.
        if row.source == "statsbomb":
            _add_case(records, row, "both_teams_sot_1h",
                      "At halftime, will both teams have at least 1 shot on target?",
                      ah1 >= 1 and bh1 >= 1)
            _add_case(records, row, "both_teams_sot_2h",
                      "Will both teams have at least 1 shot on target in the second half?",
                      ah2 >= 1 and bh2 >= 1)
            _add_case(records, row, "total_sot_2h_4plus",
                      "Will there be 4 or more total shots on target in the second half?",
                      ah2 + bh2 >= 4)
            for team, own, opp in ((a, ah2, bh2), (b, bh2, ah2)):
                _add_case(records, row, "team_more_sot_2h",
                          f"Will {team} have more shots on target than "
                          f"{b if team == a else a} in the second half?", own > opp)
                _add_case(records, row, "team_sot_2h_2plus",
                          f"Will {team} have 2 or more shots on target in the second half?", own >= 2)
        # API-Football full-match stat totals can include extra time in knockout matches. StatsBomb
        # is period-split, so its first- and second-half totals remain regulation-safe.
        regulation_stats_ok = row.stage != "knockout" or row.source == "statsbomb"
        if regulation_stats_ok:
            for threshold in range(2, 9):
                for team, total in ((a, ah1 + ah2), (b, bh1 + bh2)):
                    market = f"team_sot_full_{threshold}plus"
                    _add_case(records, row, market,
                              f"Will {team} have {threshold} or more shots on target in regulation "
                              "(90 minutes + stoppage time)?", total >= threshold)
            _add_case(records, row, "total_sot_full_8plus",
                      "Will there be 8 or more total shots on target in regulation "
                      "(90 minutes + stoppage time)?", ah1 + ah2 + bh1 + bh2 >= 8)
        # Half-card labels come from timestamped API-Football card events below. The StatsBomb
        # count table has half yellows but no corresponding half red-card clock, so using it here
        # would silently settle "card" as "yellow card" on that source.
        home_goals = float(row.home_goals_h1 + row.home_goals_h2)
        away_goals = float(row.away_goals_h1 + row.away_goals_h2)
        home_h1, away_h1 = float(row.home_goals_h1), float(row.away_goals_h1)
        home_h2, away_h2 = float(row.home_goals_h2), float(row.away_goals_h2)
        _add_case(records, row, "halftime_lead", f"At halftime, will {a} be winning?",
                  home_h1 > away_h1)
        _add_case(records, row, "halftime_lead", f"At halftime, will {b} be winning?",
                  away_h1 > home_h1)
        _add_case(records, row, "more_goals_2h",
                  "Will the second half have more total goals than the first half?",
                  home_h2 + away_h2 > home_h1 + away_h1)
        _add_case(records, row, "btts_and_total_3plus",
                  "Will both teams score AND the match have 3 or more total goals?",
                  home_goals >= 1 and away_goals >= 1 and home_goals + away_goals >= 3)
        _add_case(records, row, "win_margin_2plus",
                  f"Will {a} win by 2 or more goals in regulation (90 minutes + stoppage time)?",
                  home_goals - away_goals >= 2)
        _add_case(records, row, "win_margin_2plus",
                  f"Will {b} win by 2 or more goals in regulation (90 minutes + stoppage time)?",
                  away_goals - home_goals >= 2)

    coverage = set(
        zip(events.loc[events["event_type"] == "__match__", "source"].astype(str),
            events.loc[events["event_type"] == "__match__", "match_id"].astype(int))
    )
    event_groups = {
        (str(source), int(match_id)): group
        for (source, match_id), group in events[events["event_type"] != "__match__"].groupby(
            ["source", "match_id"], sort=False
        )
    }

    # Timestamp-derived templates.
    for row in history.itertuples(index=False):
        key = (str(row.source), int(row.match_id))
        if key not in coverage:
            continue
        event = event_groups.get(key, events.iloc[0:0])
        a, b = str(row.home_team), str(row.away_team)
        if row.source == "apifootball":
            goals = event[event["event_type"] == "goals"].copy()
            if not goals.empty:
                goals["order"] = [
                    _order_value(str(p), float(m), float(x), int(s))
                    for p, m, x, s in zip(goals.phase, goals.minute, goals.extra, goals.sequence)
                ]
            regulation = goals[goals["phase"].isin(["1H", "2H"])]
            second = goals[goals["phase"] == "2H"]
            first_side = (regulation.sort_values("order").iloc[0].team_side
                          if not regulation.empty else None)
            first_h2_side = (second.sort_values("order").iloc[0].team_side
                             if not second.empty else None)
            full_labels_ok = (
                regulation.empty or regulation.sort_values("order").iloc[0].team_side in {"home", "away"}
            )
            second_labels_ok = second.empty or first_h2_side in {"home", "away"}
            compound_labels_ok = full_labels_ok and (
                second.empty or bool(second.team_side.isin(["home", "away"]).all())
            )
            if full_labels_ok:
                for team, side in ((a, "home"), (b, "away")):
                    _add_case(records, row, "first_goal", f"Will {team} score the first goal?",
                              first_side == side)
            if second_labels_ok:
                for team, side in ((a, "home"), (b, "away")):
                    _add_case(records, row, "first_goal_2h",
                              f"Will {team} score the first goal of the second half?",
                              first_h2_side == side)
            if compound_labels_ok:
                _add_case(records, row, "first_goal_and_other_scores_2h",
                          f"Will {a} score the first goal of the game and {b} score in the second half?",
                          first_side == "home" and (second.team_side == "away").any())
                _add_case(records, row, "first_goal_and_other_scores_2h",
                          f"Will {b} score the first goal of the game and {a} score in the second half?",
                          first_side == "away" and (second.team_side == "home").any())

            is_goal = event["event_type"] == "goals"
            _add_case(records, row, "goal_before_first_hydration",
                      "Will a goal be scored before the first hydration break?",
                      (
                          is_goal
                          & (event.phase == "1H")
                          & (event.minute <= FIRST_HYDRATION_MINUTE)
                      ).any())
            _add_case(records, row, "goal_after_second_hydration_reg",
                      "Will a goal be scored after the second hydration break in regulation "
                      "(90 minutes + stoppage time)?",
                      (
                          is_goal
                          & (event.phase == "2H")
                          & (event.minute > SECOND_HYDRATION_MINUTE)
                      ).any())
            _add_case(records, row, "goal_h1_stoppage",
                      "Will a goal be scored in first-half stoppage time?",
                      (is_goal & (event.phase == "1H") & (event.extra > 0)).any())
            _add_case(records, row, "goal_h2_stoppage",
                      "Will a goal be scored in second-half stoppage time?",
                      (is_goal & (event.phase == "2H") & (event.extra > 0)).any())
            is_card = event.event_type.isin(["yellow_cards", "red_cards"])
            _add_case(records, row, "card_after_second_hydration",
                      "Will a card be shown after the second hydration break, including any extra time?",
                      (
                          is_card
                          & event.phase.isin(["2H", "ET"])
                          & (event.minute > SECOND_HYDRATION_MINUTE)
                      ).any())
            if row.stage == "knockout":
                _add_case(records, row, "red_card", "Will a red card be shown in the match?",
                          (event.event_type == "red_cards").any())
            _add_case(records, row, "card_first_half", "Will a card be shown in the first half?",
                      (is_card & (event.phase == "1H")).any())
            regulation_cards = event[is_card & event.phase.isin(["1H", "2H"])]
            sides_carded = set(regulation_cards.team_side.dropna().astype(str))
            _add_case(records, row, "both_teams_card",
                      "Will both teams receive at least one card in regulation "
                      "(90 minutes + stoppage time)?",
                      {"home", "away"}.issubset(sides_carded))
            for team, side in ((a, "home"), (b, "away")):
                _add_case(records, row, "team_card_2h_1plus",
                          f"Will {team} receive at least 1 card in the second half?",
                          (is_card & (event.phase == "2H")
                           & (event.team_side == side)).any())
            _add_case(records, row, "substitution_before_halftime",
                      "Will a substitution be made before halftime?",
                      ((event.event_type == "substitutions") & (event.phase == "1H")).any())
            regulation_phase = event.phase.isin(["1H", "2H"])
            penalty = ((event.event_type == "penalties") & regulation_phase).any()
            red = ((event.event_type == "red_cards") & regulation_phase).any()
            _add_case(records, row, "penalty_awarded",
                      "Will a penalty kick be awarded during regulation "
                      "(90 minutes + stoppage time)?", penalty)
            _add_case(records, row, "penalty_or_red",
                      "Will a penalty kick be awarded OR a red card be shown in regulation "
                      "(90 minutes + stoppage time)?", penalty or red)

        elif row.source == "statsbomb":
            early = (event.phase == "1H") & (event.minute <= 22)
            _add_case(records, row, "offside_before_first_hydration",
                      "Will either team be ruled offside before the first hydration break?",
                      ((event.event_type == "offsides") & early).any())
            _add_case(records, row, "corners_before_first_hydration_2plus",
                      "Will there be 2 or more corners before the first hydration break?",
                      int(((event.event_type == "corners") & early).sum()) >= 2)

    # Player aggregates. No observed player minutes or identities are fed to the model at prediction.
    api_lookup = {
        int(row.match_id): row for row in history[history["source"] == "apifootball"].itertuples(index=False)
    }
    for match_id, group in players.groupby("match_id", sort=False):
        row = api_lookup.get(int(match_id))
        if row is None or row.stage == "knockout" or group["team_side"].nunique() < 2:
            continue
        if bool(group["reconciles_sot"].all()):
            _add_case(records, row, "any_player_sot_2plus",
                      "Will any player record 2 or more shots on target in regulation "
                      "(90 minutes + stoppage time)?", (group["shots_on"] >= 2).any())
        _add_case(records, row, "any_player_brace",
                  "Will any player score more than 1 goal (excluding own goals) in regulation "
                  "(90 minutes + stoppage time)?", (group["goals"] >= 2).any())
        _add_case(records, row, "substitute_scores",
                  "Will a substitute score a goal in regulation (90 minutes + stoppage time)?",
                  (group["substitute"] & (group["goals"] >= 1)).any())
        if "shots_total" in group:
            total_shots = float(group["shots_total"].sum())
            for threshold in (20, 22):
                market = f"total_shots_full_{threshold}plus"
                _add_case(records, row, market,
                          f"Will there be {threshold} or more total shots (on and off target) "
                          "in regulation (90 minutes + stoppage time)?",
                          total_shots >= threshold)

    out = pd.DataFrame.from_records(records)
    for column in ("source", "home_team", "away_team", "market", "market_name", "question"):
        out[column] = out[column].astype("category")
    out["match_date"] = pd.to_datetime(out["match_date"])
    return out


def _timing_artifact(
    train: pd.DataFrame, events: pd.DataFrame, train_players: pd.DataFrame,
) -> dict:
    keys = set(zip(train["source"].astype(str), train["match_id"].astype(int)))
    mask = [key in keys for key in zip(events["source"].astype(str), events["match_id"].astype(int))]
    selected = events.loc[mask]
    event_types = {}
    for event_type in (
        "goals", "penalties", "yellow_cards", "red_cards", "substitutions", "corners", "offsides",
    ):
        subset = selected[selected["event_type"] == event_type]
        phases = {}
        for phase, group in subset.groupby("phase"):
            tokens = Counter(f"{int(r.minute)}|{int(r.extra)}" for r in group.itertuples())
            phases[str(phase)] = {"tokens": dict(sorted(tokens.items())), "n": int(len(group))}
        if phases:
            event_types[event_type] = phases

    api_train = train[train["source"] == "apifootball"]
    stage_by_id = {int(r.match_id): str(r.stage) for r in api_train.itertuples()}
    covered = set(selected.loc[(selected["source"] == "apifootball") &
                               (selected["event_type"] == "__match__"), "match_id"].astype(int))
    early_ids = set(selected.loc[(selected["event_type"] == "substitutions") &
                                 (selected["phase"] == "1H"), "match_id"].astype(int))
    totals, hits = Counter(), Counter()
    for match_id in covered:
        stage = stage_by_id.get(match_id, "all")
        totals["all"] += 1; totals[stage] += 1
        if match_id in early_ids:
            hits["all"] += 1; hits[stage] += 1
    rates = {key: (hits[key] + 1.0) / (totals[key] + 2.0) for key in totals}

    sub_share = 0.12
    played = train_players[train_players["minutes"] > 0]
    total_goals = float(played["goals"].sum()) if len(played) else 0.0
    if total_goals > 0:
        sub_goals = float(played.loc[played["substitute"], "goals"].sum())
        sub_share = (sub_goals + 5.0 * 0.12) / (total_goals + 5.0)
    goals = int((selected["event_type"] == "goals").sum())
    own_goals = int((selected["event_type"] == "own_goals").sum())
    from .shots import fit_total_shots_model

    return {
        "schema_version": 1, "created_at": str(train["match_date"].max()),
        "n_event_matches": len(covered), "event_types": event_types,
        "binary_rates": {"substitution_before_halftime": rates},
        "parameters": {
            "substitute_goal_share": sub_share,
            "own_goal_share": (own_goals + 1.0) / (goals + 2.0),
        },
        "models": {"total_shots": fit_total_shots_model(train_players, train)},
    }


def _fold_settings(
    settings: Settings, train: pd.DataFrame, events: pd.DataFrame,
    players: pd.DataFrame, player_year: pd.Series, fold_year: int, root: Path,
) -> Settings:
    ratings = fit_team_ratings(results_from_stat_table(train))
    ratings_path = root / "team_ratings.parquet"
    ratings.save(ratings_path)
    artifact_path, metadata_path = root / "rate_model.joblib", root / "rate_model.json"
    train_rate_models(train, ratings, settings, artifact_path=artifact_path,
                      metadata_path=metadata_path)

    train_players = players[(player_year > 0) & (player_year < fold_year)]
    shares = pd.concat([
        fit_shares(train_players, settings, stat="shots_on_target"),
        fit_shares(train_players, settings, stat="goals"),
        fit_shares(train_players, settings, stat="assists"),
    ], ignore_index=True)
    shares_path = root / "player_shares.json"
    write_shares(shares, shares_path)
    timing_path = root / "event_timing.json"
    timing_path.write_text(json.dumps(_timing_artifact(train, events, train_players), sort_keys=True))

    return _settings_from_fold_artifacts(settings, root)


def _settings_from_fold_artifacts(settings: Settings, root: Path) -> Settings:
    raw = copy.deepcopy(settings.raw)
    raw["rates"]["model"] = "learned"
    raw["rates"]["learned"].update({
        "artifact": str(root / "rate_model.joblib"),
        "metadata": str(root / "rate_model.json"),
        "team_ratings": str(root / "team_ratings.parquet"),
    })
    raw.setdefault("postsim", {}).update({
        "enabled": True, "event_model": str(root / "event_timing.json"),
        "player_allocation": True, "player_shares": str(root / "player_shares.json"),
    })
    return Settings(raw=raw, market_rules=settings.market_rules, root=settings.root)


def prepare_fold_artifacts(
    settings: Settings | None = None, *, test_years: tuple[int, ...] = DEFAULT_TEST_YEARS,
    out_root: str | Path, history_path: str | Path = HISTORY_TABLE,
    event_path: str | Path = EVENT_TABLE, player_path: str | Path = PLAYER_TABLE,
) -> None:
    """Train leakage-safe fold artifacts separately so scoring can run in a fresh low-RAM process."""
    settings = settings or default_settings()
    history = point_in_time_elos(pd.read_parquet(settings.path(history_path)))
    history["year"] = pd.to_datetime(history["match_date"], errors="coerce").dt.year
    events = pd.read_parquet(settings.path(event_path))
    players = pd.read_parquet(settings.path(player_path), columns=[
        "match_id", "tournament", "player", "position", "minutes", "substitute",
        "shots_total", "shots_on", "goals", "assists", "reconciles_sot", "team_side", "team",
    ])
    match_year = history.loc[history["source"] == "apifootball"].set_index("match_id")["year"]
    player_year = players["match_id"].map(match_year).fillna(0).astype(int)
    root = settings.path(out_root)
    for year in test_years:
        train = history[history["year"] < year].copy()
        if len(train) < 500:
            continue
        target = root / str(year)
        target.mkdir(parents=True, exist_ok=True)
        print(f"[exotic-oos] prepare fold={year} train_matches={len(train)} -> {target}", flush=True)
        _fold_settings(settings, train, events, players, player_year, year, target)
        _release_heap()


def _release_heap() -> None:
    """Return completed sklearn/pandas scratch allocations to the OS on small live hosts."""
    gc.collect()
    try:
        import ctypes

        ctypes.CDLL("libc.so.6").malloc_trim(0)
    except Exception:
        pass


def _flush_score_buffer(buffer: list[dict], root: Path, index: int) -> Path:
    path = root / f"scores_{index:05d}.parquet"
    pd.DataFrame.from_records(buffer).to_parquet(path, index=False)
    buffer.clear()
    _release_heap()
    return path


def _direct_standard_fallback(engine, ctx, question_row, n_sims: int):
    """Resolve a narrow set of count props when the frozen parser confuses nested team names.

    For example, ``Guinea`` is a substring of ``Equatorial Guinea``.  This uses exactly the same
    simulated outcome and resolver definitions; only team selection bypasses the baseline parser.
    """
    market = str(question_row.market)
    if market not in {
        "team_more_sot_2h", "team_sot_2h_2plus", "team_card_2h_1plus",
    } and not market.startswith("team_sot_full_"):
        return None
    outcome = engine._simulate(ctx, n_sims)
    question = str(question_row.question)
    team = TEAM_A if question.startswith(f"Will {ctx.team_a} ") else TEAM_B
    if market == "team_more_sot_2h":
        other = TEAM_B if team == TEAM_A else TEAM_A
        hit = (outcome.team_half(SHOTS_ON_TARGET, team, H2)
               > outcome.team_half(SHOTS_ON_TARGET, other, H2))
    elif market == "team_sot_2h_2plus":
        hit = outcome.team_half(SHOTS_ON_TARGET, team, H2) >= 2
    elif market == "team_card_2h_1plus":
        hit = outcome.team_half(YELLOWS, team, H2) >= 1
    else:
        threshold = int(market.removeprefix("team_sot_full_").removesuffix("plus"))
        hit = outcome.team_total(SHOTS_ON_TARGET, team, include_et=False) >= threshold
    return SimpleNamespace(p_final=float(np.mean(hit)))


def _empirical_summary(questions: pd.DataFrame) -> pd.DataFrame:
    return (
        questions.groupby("contract_key", as_index=False, observed=True)
        .agg(market=("market", "first"), market_name=("market_name", "first"),
             n_all=("outcome", "size"), matches_all=("match_id", "nunique"),
             empirical_rate=("outcome", "mean"))
        .sort_values("empirical_rate")
    )


def all_data_empirical_rates(
    settings: Settings | None = None, *, history_path: str | Path = HISTORY_TABLE,
    event_path: str | Path = EVENT_TABLE, player_path: str | Path = PLAYER_TABLE,
) -> pd.DataFrame:
    """Empirical base rates over every eligible match in the shipped 2015–2026 history."""
    settings = settings or default_settings()
    history = pd.read_parquet(settings.path(history_path))
    events = pd.read_parquet(settings.path(event_path))
    players = pd.read_parquet(settings.path(player_path), columns=[
        "match_id", "team_side", "reconciles_sot", "shots_total", "shots_on", "goals",
        "substitute",
    ])
    return _empirical_summary(build_question_table(history, events, players))


def tournament_empirical_rates(
    settings: Settings | None = None, *, tournament: str,
    history_path: str | Path = HISTORY_TABLE, event_path: str | Path = EVENT_TABLE,
    player_path: str | Path = PLAYER_TABLE,
) -> pd.DataFrame:
    """Empirical rates over every exact-labelable match currently shipped for one tournament."""
    settings = settings or default_settings()
    history = pd.read_parquet(settings.path(history_path))
    history = history[history.tournament.astype(str).eq(tournament)].copy()
    keys = set(zip(history.source.astype(str), history.match_id.astype(int)))
    events = pd.read_parquet(settings.path(event_path))
    events = events.loc[
        [key in keys for key in zip(events.source.astype(str), events.match_id.astype(int))]
    ]
    api_ids = set(history.loc[history.source.eq("apifootball"), "match_id"].astype(int))
    players = pd.read_parquet(settings.path(player_path), columns=[
        "match_id", "team_side", "reconciles_sot", "shots_total", "shots_on", "goals",
        "substitute",
    ])
    players = players[players.match_id.isin(api_ids)]
    result = _empirical_summary(build_question_table(history, events, players))
    result["tournament"] = tournament
    result["data_matches"] = int(history[["source", "match_id"]].drop_duplicates().shape[0])
    result["data_through"] = str(pd.to_datetime(history.match_date).max().date())
    return result


def run_rolling_backtest(
    settings: Settings | None = None, *, n_sims: int = 1000,
    test_years: tuple[int, ...] = DEFAULT_TEST_YEARS,
    history_path: str | Path = HISTORY_TABLE, event_path: str | Path = EVENT_TABLE,
    player_path: str | Path = PLAYER_TABLE, all_data_empirical: bool = True,
    fold_artifact_root: str | Path | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Return question rows, per-market Brier summary, and all-data empirical rates."""
    settings = settings or default_settings()
    history = point_in_time_elos(pd.read_parquet(settings.path(history_path)))
    history["year"] = pd.to_datetime(history["match_date"], errors="coerce").dt.year
    events = pd.read_parquet(settings.path(event_path))
    players = pd.read_parquet(settings.path(player_path), columns=[
        "match_id", "tournament", "player", "position", "minutes", "substitute",
        "shots_total", "shots_on", "goals", "assists", "reconciles_sot", "team_side", "team",
    ])
    label_history = (
        history if all_data_empirical
        else history[history["year"].isin(test_years)]
    )
    label_keys = set(zip(label_history["source"].astype(str), label_history["match_id"].astype(int)))
    label_events = events.loc[
        [key in label_keys for key in zip(events["source"].astype(str),
                                           events["match_id"].astype(int))]
    ]
    label_player_ids = set(
        label_history.loc[label_history["source"] == "apifootball", "match_id"].astype(int)
    )
    label_players = players[players["match_id"].isin(label_player_ids)]
    questions = build_question_table(label_history, label_events, label_players)

    match_year = history.loc[history["source"] == "apifootball"].set_index("match_id")["year"]
    player_year = players["match_id"].map(match_year).fillna(0).astype(int)
    empirical = _empirical_summary(questions)

    scored: list[dict] = []
    score_paths: list[Path] = []
    expected_scored = 0
    history_lookup = {
        (str(r.source), int(r.match_id)): r for r in history.itertuples(index=False)
    }
    with tempfile.TemporaryDirectory(prefix="exotic_oos_scores_") as score_tmp:
        score_root = Path(score_tmp)
        for year in test_years:
            train = history[history["year"] < year].copy()
            test_questions = questions[questions["year"] == year]
            if len(train) < 500 or test_questions.empty:
                continue
            expected_scored += len(test_questions)
            print(f"[exotic-oos] fold={year} train_matches={len(train)} "
                  f"test_matches={test_questions.match_id.nunique()} questions={len(test_questions)}",
                  flush=True)
            artifact_dir = (
                settings.path(fold_artifact_root) / str(year) if fold_artifact_root else None
            )
            artifact_context = (
                contextlib.nullcontext(str(artifact_dir)) if artifact_dir
                else tempfile.TemporaryDirectory(prefix=f"exotic_oos_{year}_")
            )
            with artifact_context as tmp:
                if artifact_dir:
                    if not (artifact_dir / "rate_model.joblib").exists():
                        raise FileNotFoundError(f"missing prepared fold artifacts: {artifact_dir}")
                    fold_settings = _settings_from_fold_artifacts(settings, artifact_dir)
                else:
                    fold_settings = _fold_settings(
                        settings, train, events, players, player_year, year, Path(tmp)
                    )
                _release_heap()
                engine = build_engine(fold_settings)
                for (source, match_id), group in test_questions.groupby(
                    ["source", "match_id"], sort=False
                ):
                    row = history_lookup[(str(source), int(match_id))]
                    ctx = MatchContext(
                        str(row.home_team), str(row.away_team),
                        elo_a=float(row.home_elo), elo_b=float(row.away_elo),
                        stage=str(row.stage), host_a=bool(row.home_host), host_b=bool(row.away_host),
                    )
                    try:
                        predictions = engine.predict_many(
                            ctx, group["question"].tolist(), n_sims=n_sims
                        )
                    except Exception as exc:
                        print(f"  batch fallback {source}:{match_id}: {exc!r}", flush=True)
                        pairs = []
                        for _, question_row in group.iterrows():
                            try:
                                pairs.append((question_row, engine.predict(
                                    ctx, str(question_row.question), n_sims=n_sims
                                )))
                            except Exception as item_exc:
                                prediction = _direct_standard_fallback(
                                    engine, ctx, question_row, n_sims
                                )
                                if prediction is None:
                                    print(f"    skip {question_row.market}: {item_exc!r}", flush=True)
                                else:
                                    pairs.append((question_row, prediction))
                    else:
                        pairs = [
                            (question_row, prediction)
                            for (_, question_row), prediction in zip(group.iterrows(), predictions)
                        ]
                    for question_row, prediction in pairs:
                        p = float(prediction.p_final)
                        y = int(question_row.outcome)
                        scored.append({
                            **question_row.to_dict(), "fold_year": year,
                            "p_model": p, "model_brier": (p - y) ** 2,
                            "p_no_knowledge": 0.5, "no_knowledge_brier": 0.25,
                        })
                    if len(scored) >= 1000:
                        score_paths.append(_flush_score_buffer(
                            scored, score_root, len(score_paths)
                        ))
                    # The engine caches full outcome arrays; one match at a time keeps peak RAM flat.
                    engine._sim_cache.clear()
                    engine._timeline_cache.clear()
                    engine._event_cache.clear()
                if scored:
                    score_paths.append(_flush_score_buffer(scored, score_root, len(score_paths)))
                del engine, fold_settings
                _release_heap()
        rows = (
            pd.concat((pd.read_parquet(path) for path in score_paths), ignore_index=True)
            if score_paths else pd.DataFrame()
        )
    if rows.empty:
        raise RuntimeError("rolling backtest produced no scored questions")
    if len(rows) != expected_scored:
        raise RuntimeError(
            f"rolling backtest scored {len(rows)} of {expected_scored} questions; see skip logs"
        )
    summary = (
        rows.groupby(["contract_key", "market", "market_name"], as_index=False)
        .agg(n=("outcome", "size"), matches=("match_id", "nunique"),
             test_years=("fold_year", "nunique"), empirical_rate_test=("outcome", "mean"),
             mean_model_probability=("p_model", "mean"), model_brier=("model_brier", "mean"),
             no_knowledge_brier=("no_knowledge_brier", "mean"))
    )
    summary["delta_model_minus_50"] = summary.model_brier - summary.no_knowledge_brier
    summary["model_beats_50"] = summary.delta_model_minus_50 < 0
    return rows, summary.sort_values("delta_model_minus_50"), empirical


def _main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Rolling-origin Brier backtest for exotic markets")
    ap.add_argument("--prepare-events", action="store_true")
    ap.add_argument("--event-table", default=EVENT_TABLE)
    ap.add_argument("--n-sims", type=int, default=1000)
    ap.add_argument("--test-years", default=",".join(map(str, DEFAULT_TEST_YEARS)))
    ap.add_argument("--out-dir", default="notebooks")
    ap.add_argument(
        "--fold-only-labels", action="store_true",
        help="build labels only for --test-years (lower memory; empirical CSV is not all-history)",
    )
    ap.add_argument(
        "--prepare-fold-artifacts", metavar="DIR",
        help="train year-fold artifacts into DIR and exit; score later with --fold-artifacts",
    )
    ap.add_argument(
        "--fold-artifacts", metavar="DIR",
        help="score with previously prepared year-fold artifacts instead of training in-process",
    )
    ap.add_argument(
        "--empirical-only", action="store_true",
        help="write the all-history empirical-rate CSV without fitting or scoring folds",
    )
    args = ap.parse_args(argv)
    settings = default_settings()
    event_path = settings.path(args.event_table)
    if args.prepare_events or not event_path.exists():
        table = build_event_table(settings, out_path=event_path)
        print(f"[exotic-oos] event rows={len(table)} -> {event_path}")
    years = tuple(int(value) for value in args.test_years.split(",") if value.strip())
    if args.empirical_only:
        out = settings.path(args.out_dir)
        out.mkdir(parents=True, exist_ok=True)
        empirical = all_data_empirical_rates(settings, event_path=event_path)
        empirical.to_csv(out / "exotic_empirical_rates.csv", index=False)
        print(empirical.to_string(index=False))
        return 0
    if args.prepare_fold_artifacts:
        prepare_fold_artifacts(
            settings, test_years=years, out_root=args.prepare_fold_artifacts,
            event_path=event_path,
        )
        return 0
    rows, summary, empirical = run_rolling_backtest(
        settings, n_sims=args.n_sims, test_years=years, event_path=event_path,
        all_data_empirical=not args.fold_only_labels,
        fold_artifact_root=args.fold_artifacts,
    )
    out = settings.path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    rows.to_csv(out / "exotic_oos_rows.csv", index=False)
    summary.to_csv(out / "exotic_oos_summary.csv", index=False)
    empirical.to_csv(out / "exotic_empirical_rates.csv", index=False)
    print(summary.to_string(index=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
