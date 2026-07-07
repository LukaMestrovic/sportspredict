"""Compact, question-scoped simulator evidence.

The report deliberately omits rate tensors, sampled outcomes and unrelated
markets. Each requested question receives one YES probability and one
deterministic sentence describing the model basis.
"""

from __future__ import annotations

import csv
from collections.abc import Iterable
from typing import Any

from sportspredict.config import Settings, default_settings
from sportspredict.features.context import MatchContext, PlayerInfo

from .engine import SimulatorEngine, build_engine
from .postsim import (
    ANY_PLAYER_THRESHOLD,
    BOTH_TEAMS_CARD,
    CARD_WINDOW,
    CARDS_MORE_THAN_GOALS,
    COMPOUND_AND,
    FIRST_GOAL,
    GOAL_WINDOW,
    LEAD_ANY_TIME,
    PLAYER_FULL_MATCH,
    RED_CARD,
    STAT_WINDOW,
    SUBSTITUTE_SCORE,
    SUBSTITUTION_BEFORE_HALF,
    SECOND_HYDRATION_MINUTE,
    TOTAL_SHOTS_THRESHOLD,
    WIN_MARGIN,
    parse_extended,
)
from .postsim.contracts import contract_key
from .postsim.evidence import HistoricalEvidence
from .teams import canonical_name

SCHEMA_VERSION = "2.2"
EVIDENCE_ROLE = (
    "Model context only: this is a deterministic simulator estimate, not a final submission price."
)


def _question_items(questions: Iterable[str | dict[str, Any]]) -> list[dict[str, str]]:
    items = []
    for index, item in enumerate(questions, 1):
        if isinstance(item, str):
            question, market_id = item, f"q{index}"
        else:
            question = str(item.get("question") or "").strip()
            market_id = str(item.get("market_id") or item.get("id") or f"q{index}")
        if question:
            items.append({"market_id": market_id, "question": question})
    return items


def _simulator_can_parse(item: dict[str, str], ctx: MatchContext) -> bool:
    try:
        if parse_extended(item["question"], ctx) is not None:
            return True
    except Exception:
        pass
    try:
        from sportspredict.markets import parse_question

        parse_question(item["question"], ctx)
        return True
    except Exception:
        return False


def _explanation(market: str, params: dict, notes: str | None) -> str:
    """One deterministic, family-specific sentence; no generated prose or hidden heuristics."""
    if market == FIRST_GOAL:
        scope = "including extra time" if params.get("include_et") else "regulation only"
        return (
            "Estimated from learned team/half goal counts and historical goal timing, with the "
            f"first scorer resolved inside each shared simulated match world ({scope})."
        )
    if market == GOAL_WINDOW:
        if params.get("window") == "before_first_hydration":
            return (
                "Estimated from learned regulation goal counts and historical goal clocks through "
                "minute 22, the first hydration-break boundary."
            )
        if params.get("window") == "after_first_hydration_1h":
            return (
                "Estimated from learned first-half goal counts and historical goal clocks after "
                "minute 22, the first hydration-break boundary, through first-half stoppage time."
            )
        if params.get("window") == "after_second_hydration":
            scope = "including extra time" if params.get("include_et") else "regulation only"
            return (
                "Estimated from learned goal counts and historical goal clocks strictly after "
                f"minute {SECOND_HYDRATION_MINUTE:.0f} ({scope})."
            )
        if params.get("window") == "stoppage":
            return (
                f"Estimated only from goals carrying a positive {params.get('half')} added-time "
                "clock; ordinary goals in that half are excluded."
            )
        return (
            "Estimated from learned goal counts and the historical within-half goal-time "
            "distribution for this exact clock window."
        )
    if market == CARD_WINDOW:
        if params.get("window") == "first_half":
            return (
                "Estimated from simulated yellow and red cards whose learned event clocks fall "
                "in the regulation first half, including first-half added time."
            )
        return (
            "Estimated from simulated yellow/red-card volume and the historical late-card timing "
            "distribution for this exact clock window."
        )
    if market == RED_CARD:
        return "Estimated from simulated red-card counts, with regulation and extra time separated."
    if market == BOTH_TEAMS_CARD:
        return "Estimated when each team has at least one simulated yellow or red card in regulation."
    if market == LEAD_ANY_TIME:
        return (
            "Estimated by walking the simulated goal timeline in each shared match world "
            "and checking whether the named team ever holds a lead."
        )
    if market == CARDS_MORE_THAN_GOALS:
        return (
            "Estimated inside each shared simulated match world by comparing total "
            "yellow/red cards with total goals over the question's exact time scope."
        )
    if market == PLAYER_FULL_MATCH:
        return (
            "Estimated from confirmed lineup exposure, expected minutes, and position-level "
            "full-match completion rates; it is model context, not a bookmaker quote."
        )
    if market == "team_score_no_own":
        return (
            "Estimated from the named team's simulated goal count after removing the learned "
            "own-goal share, over the question's exact regulation/full-match scope."
        )
    if market == TOTAL_SHOTS_THRESHOLD:
        return (
            "Estimated from simulated regulation shots on target plus a historical conditional "
            "off-target-shot model."
        )
    if market == WIN_MARGIN:
        return "Estimated from the regulation goal difference in each shared simulated match world."
    if market == STAT_WINDOW:
        stat = str(params.get("stat") or "event").replace("_", " ")
        learned = "source-gated learned" if stat == "offsides" else "learned"
        return (
            f"Estimated from {learned} {stat} totals and their historical within-half timing "
            "distribution for this exact clock window."
        )
    if market == SUBSTITUTION_BEFORE_HALF:
        return (
            "Estimated from the beta-smoothed historical frequency of a substitution before "
            "halftime, using the match stage where available."
        )
    if market == SUBSTITUTE_SCORE:
        return (
            "Estimated by assigning simulated regulation goals to substitutes using the learned "
            "historical substitute goal share and available lineup exposure."
        )
    if market == ANY_PLAYER_THRESHOLD:
        return (
            "Estimated by allocating each simulated team total across players with learned shares "
            "and evaluating the exact any-player occupancy probability."
        )
    if market == COMPOUND_AND:
        return (
            "Estimated as a joint event inside the same simulated match worlds, preserving the "
            "dependence between both component conditions."
        )

    lower = str(market).lower()
    if notes and "post-sim allocation" in notes:
        return (
            "Estimated by allocating the simulated team total to the named player using learned "
            "player shares and expected-minutes exposure."
        )
    if "penalt" in lower or "red" in lower:
        return (
            "Estimated from the learned match context and the simulator's shared physicality "
            "worlds for rare disciplinary events."
        )
    if "card" in lower or "yellow" in lower:
        return (
            "Estimated from learned team/half card rates in correlated match simulations, including "
            "match-stage and physicality effects."
        )
    if "corner" in lower:
        return (
            "Estimated from learned team/half corner rates in the same correlated simulated match "
            "worlds used for the other questions."
        )
    if "shot" in lower:
        return (
            "Estimated from learned team/half shot-on-target rates in the same correlated simulated "
            "match worlds used for the other questions."
        )
    if "goal" in lower or "score" in lower or "btts" in lower:
        return (
            "Estimated from learned team/half goal rates in the same correlated simulated match "
            "worlds used for the other questions."
        )
    return (
        "Estimated from the learned-rate Monte Carlo match model, preserving shared match-state "
        "dependence across teams, halves and event families."
    )


def build_simulation_report(
    ctx: MatchContext,
    questions: Iterable[str | dict[str, Any]],
    *,
    engine: SimulatorEngine | None = None,
    settings: Settings | None = None,
    n_sims: int | None = None,
) -> dict[str, Any]:
    """Price only ``questions`` and return the compact simulator report."""
    settings = settings or (engine.settings if engine is not None else default_settings())
    engine = engine or build_engine(settings)
    items = _question_items(questions)
    eligible = [item for item in items if _simulator_can_parse(item, ctx)]
    unsupported = [
        {**item, "reason": "No simulator resolver for this exact question template."}
        for item in items if item not in eligible
    ]
    report_cfg = settings.raw.get("postsim", {})
    requested_sims = max(1, int(n_sims or report_cfg.get("report_n_sims", 8000)))
    effective_sims = min(requested_sims, int(report_cfg.get("report_max_n_sims", 10000)))
    historical = HistoricalEvidence.load(
        settings.path(report_cfg.get("evidence_history", "data/processed/simulation_evidence.json"))
    )

    predictions: list[Any | None] = []
    try:
        if eligible:
            predictions = list(engine.predict_many(
                ctx, [item["question"] for item in eligible], n_sims=effective_sims,
            ))
    except Exception:
        # One unsupported question must not erase the usable evidence for its neighbours.
        predictions = []
        for item in eligible:
            try:
                predictions.append(engine.predict(
                    ctx, item["question"], n_sims=effective_sims,
                ))
            except Exception:
                predictions.append(None)

    reports = []
    for item, pred in zip(eligible, predictions):
        if pred is None:
            unsupported.append({
                **item,
                "reason": "No simulator resolver for this exact question template.",
            })
            continue
        probability = float(pred.p_final)
        key = contract_key(str(pred.market), pred.params or {}, stage=ctx.stage)
        reports.append({
            **item,
            "source": "sportspredict-simulator",
            "family": str(pred.market),
            "contract_key": key,
            "probability": round(probability, 6),
            "probability_pct": round(probability * 100.0, 2),
            "explanation": _explanation(str(pred.market), pred.params or {}, pred.notes),
            "historical_evidence": historical.get(key, family=str(pred.market)),
            "evidence_role": "model_context",
        })

    used_sims = next((int(p.n_sims) for p in predictions if p is not None), effective_sims)
    return {
        "schema_version": SCHEMA_VERSION,
        "match": {"team_a": ctx.team_a, "team_b": ctx.team_b, "stage": ctx.stage},
        "model": {
            "engine": type(engine).__name__,
            "rate_model": type(engine.rate_model).__name__,
            "n_sims": used_sims,
        },
        "evidence_instruction": EVIDENCE_ROLE,
        "question_reports": reports,
        "unsupported_questions": unsupported,
    }


def _lineup_players(raw: Any, team: str) -> list[PlayerInfo]:
    if not isinstance(raw, list):
        return []
    players = []
    for item in raw:
        if isinstance(item, str):
            item = {"name": item}
        if not isinstance(item, dict):
            continue
        nested = item.get("player") if isinstance(item.get("player"), dict) else {}
        name = str(item.get("name") or nested.get("name") or "").strip()
        if not name:
            continue
        position = str(item.get("position") or item.get("pos") or "MF").upper()
        position = {"G": "GK", "D": "DF", "M": "MF", "F": "FW"}.get(position, position)
        players.append(PlayerInfo(
            name=name,
            team=team,
            position=position if position in {"GK", "DF", "MF", "FW"} else "MF",
            goal_rate=item.get("goal_rate"),
            assist_rate=item.get("assist_rate"),
            penalty_taker=bool(item.get("penalty_taker", False)),
            start_prob=float(item.get("start_prob", 1.0)),
            expected_minutes=(
                float(item["expected_minutes"])
                if item.get("expected_minutes") is not None else None
            ),
        ))
    return players


def context_from_payload(payload: dict[str, Any], settings: Settings | None = None) -> MatchContext:
    """Build the same match context used by the compete loop from a bridge JSON payload."""
    settings = settings or default_settings()
    home = str(payload.get("home") or payload.get("team_a") or "").strip()
    away = str(payload.get("away") or payload.get("team_b") or "").strip()
    if not home or not away:
        raise ValueError("payload requires home/team_a and away/team_b")

    elo_table = _load_elo_table(settings.path("data/raw/elo.csv"))
    canonical_home = canonical_name(home, elo_table)
    canonical_away = canonical_name(away, elo_table)
    tournament = settings.raw.get("tournament", {})
    hosts = set(tournament.get("host_teams", []))
    kickoff = str(payload.get("kickoff") or "")
    knockout_after = str(tournament.get("group_stage_end") or "")
    ctx = MatchContext(
        canonical_home,
        canonical_away,
        elo_a=float(elo_table.get(canonical_home, 1500.0)),
        elo_b=float(elo_table.get(canonical_away, 1500.0)),
        stage="knockout" if kickoff and knockout_after and kickoff >= knockout_after else "group",
        host_a=canonical_home in hosts,
        host_b=canonical_away in hosts,
        date=kickoff or None,
    )
    ctx.extra["aliases"] = {
        "A": [name for name in (home, canonical_home) if name],
        "B": [name for name in (away, canonical_away) if name],
    }

    if payload.get("stage"):
        ctx.stage = str(payload["stage"])
    if payload.get("elo_a") is not None:
        ctx.elo_a = float(payload["elo_a"])
    if payload.get("elo_b") is not None:
        ctx.elo_b = float(payload["elo_b"])
    if payload.get("referee"):
        ctx.referee = str(payload["referee"])

    lineups = payload.get("lineups") or {}
    if isinstance(lineups, dict):
        ctx.lineup_a = _lineup_players(
            lineups.get("A") or lineups.get("home") or lineups.get(home)
            or lineups.get(canonical_home), ctx.team_a,
        )
        ctx.lineup_b = _lineup_players(
            lineups.get("B") or lineups.get("away") or lineups.get(away)
            or lineups.get(canonical_away), ctx.team_b,
        )
    elif isinstance(lineups, list):
        # Native API-Football fixtures/lineups response used by the parent bot.
        by_team: dict[str, list[dict]] = {}
        for entry in lineups:
            if not isinstance(entry, dict):
                continue
            team_name = str((entry.get("team") or {}).get("name") or "")
            rows = []
            for key, start_prob, minutes in (
                ("startXI", 1.0, 82.0), ("substitutes", 0.2, 20.0),
            ):
                for slot in entry.get(key) or []:
                    player = slot.get("player") or {}
                    rows.append({
                        "name": player.get("name"), "position": player.get("pos") or "MF",
                        "start_prob": start_prob, "expected_minutes": minutes,
                    })
            by_team[team_name] = rows
        ordered = list(by_team.values())
        ctx.lineup_a = _lineup_players(
            by_team.get(home) or by_team.get(canonical_home)
            or by_team.get(ctx.team_a) or (ordered[0] if ordered else []), ctx.team_a,
        )
        ctx.lineup_b = _lineup_players(
            by_team.get(away) or by_team.get(canonical_away) or by_team.get(ctx.team_b)
            or (ordered[1] if len(ordered) > 1 else []), ctx.team_b,
        )
    return ctx


def _load_elo_table(path) -> dict[str, float]:
    """Load the bundled two-column Elo snapshot without a dataframe dependency."""
    try:
        with open(path, encoding="utf-8", newline="") as handle:
            return {
                str(row["team"]): float(row["rating"])
                for row in csv.DictReader(handle)
                if row.get("team") and row.get("rating")
            }
    except (OSError, KeyError, TypeError, ValueError):
        return {}


def simulation_report_from_payload(
    payload: dict[str, Any], *, settings: Settings | None = None,
) -> dict[str, Any]:
    """JSON bridge used by the parent bot and the CLI."""
    settings = settings or default_settings()
    ctx = context_from_payload(payload, settings)
    return build_simulation_report(
        ctx,
        payload.get("questions") or [],
        settings=settings,
        n_sims=payload.get("n_sims"),
    )
