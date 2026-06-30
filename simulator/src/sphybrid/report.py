"""Compact, question-scoped simulator evidence for an external LLM pricing layer.

The report deliberately omits rate tensors, sampled outcomes and unrelated markets.  Each requested
question receives one YES probability and one deterministic sentence describing the model basis.
That is enough for the LLM to challenge the estimate against odds and live context without asking it
to reverse-engineer the simulator.
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
    COMPOUND_AND,
    FIRST_GOAL,
    GOAL_WINDOW,
    RED_CARD,
    STAT_WINDOW,
    SUBSTITUTE_SCORE,
    SUBSTITUTION_BEFORE_HALF,
    TOTAL_SHOTS_THRESHOLD,
    WIN_MARGIN,
    parse_extended,
)
from .postsim.contracts import contract_key
from .postsim.evidence import HistoricalEvidence
from .teams import canonical_name

SCHEMA_VERSION = "2.0"
EVIDENCE_ROLE = (
    "Model context only: weigh this estimate against disclosed conditioning inputs, confirmed lineups, "
    "tactics, game state, referee and information freshness."
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
        return (
            "Estimated from learned team/half goal counts and historical goal timing, with the "
            "first scorer resolved inside each shared simulated match world."
        )
    if market == GOAL_WINDOW:
        if params.get("window") == "before_first_hydration":
            return (
                "Estimated from learned regulation goal counts and historical goal clocks through "
                "minute 22, the first hydration-break boundary."
            )
        if params.get("window") == "after_second_hydration":
            scope = "including extra time" if params.get("include_et") else "regulation only"
            return (
                "Estimated from learned goal counts and historical goal clocks strictly after "
                f"minute 67 ({scope})."
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


def _adjustment_guidance(market: str, params: dict, question: str) -> str:
    """Deterministic pre-match directions for the web-grounded LLM layer."""
    lower = question.lower()
    if market == CARD_WINDOW and params.get("window") == "after_second_hydration":
        if params.get("include_et"):
            return (
                "Nudge upward when regulation draw odds rise because extra time becomes more likely; "
                "also raise for higher card-total odds, a card-prone referee, combative teams and a "
                "close knockout, and lower for a lenient referee or low card totals."
            )
        return (
            "Raise for higher regulation card totals, a card-prone referee, a close score expectation "
            "and strong late-game stakes; do not add an extra-time uplift because this contract ends "
            "after regulation stoppage time."
        )
    if market == GOAL_WINDOW:
        window = params.get("window")
        if window == "before_first_hydration":
            return (
                "Raise with higher goal-total odds, aggressive starting lineups, weak early defending "
                "and fast-start tactical evidence; lower for conservative shapes or key-attacker absences."
            )
        if window == "after_second_hydration":
            if params.get("include_et"):
                return (
                    "This contract includes regulation after minute 67 plus any extra time. "
                    "Calibrate the simulator against the overall, current-WC and knockout-stage "
                    "empirical rates, weighted by sample size rather than averaged. Explicitly "
                    "use the de-vigged regulation-draw probability: a higher draw probability "
                    "raises extra-time exposure and therefore the chance of a later goal. Also "
                    "raise for higher goal totals, attacking benches and likely late chasing; "
                    "lower for low totals and defensive benches. Do not double-count draw odds "
                    "if they are already reflected in the estimate."
                )
            return (
                "Raise with higher goal totals, strong attacking benches, a close match that may require "
                "late chasing, and vulnerable late defenses; lower for low totals and defensive benches."
                " Do not use extra-time likelihood because this contract is explicitly regulation-only."
            )
        return (
            "Raise with higher goal totals and evidence for long added time (VAR, injuries, time-wasting "
            "or a likely late chase); do not treat the whole half as stoppage time."
        )
    if market == STAT_WINDOW and params.get("stat") == "offsides":
        return (
            "Raise for high defensive lines, direct runners and higher team-offside odds; lower for deep "
            "blocks, slow buildup or absent pace forwards."
        )
    if market == STAT_WINDOW and params.get("stat") == "corners":
        return (
            "Raise for high corner totals, a territorial favorite and wing/cross-heavy tactics; lower for "
            "central possession, low shot volume or a balanced territorial matchup."
        )
    if market == SUBSTITUTION_BEFORE_HALF:
        return (
            "Raise for pre-match injury doubts, heat, tactical mismatch risk or coaches known for early "
            "changes; lower when both starting elevens are healthy and tactically stable."
        )
    if market == SUBSTITUTE_SCORE:
        return (
            "Raise for a deep attacking bench, likely high-impact substitutes, higher goal totals and a "
            "game state likely to require chasing; lower for weak benches or low expected scoring."
        )
    if market == ANY_PLAYER_THRESHOLD and params.get("stat") == "goals":
        return (
            "Raise when team-goal odds are high and scorer probability is concentrated in one or two "
            "90-minute penalty-taking forwards; lower when goals are spread across rotation risks."
        )
    if market in {RED_CARD, BOTH_TEAMS_CARD} or "card" in market or "card" in lower:
        return (
            "Calibrate against the overall, current-WC and knockout-stage empirical rates, weighted "
            "by sample size rather than averaged. Raise for direct red-card evidence, a referee with "
            "high red-card incidence, team red-card/foul profiles and genuine knockout tension; lower "
            "for a lenient referee and low-contact matchup. For match-scope knockout contracts, higher "
            "regulation-draw odds modestly raise extra-time exposure. Do not treat ordinary yellow-card "
            "totals as equivalent to red-card risk."
        )
    if "penalt" in market or "penalty" in lower:
        return (
            "Raise for higher penalty odds, frequent box entries, dribblers, VAR-sensitive defending and "
            "a penalty-prone referee; lower for low attacking volume and disciplined defenses."
        )
    if market in {FIRST_GOAL, COMPOUND_AND, WIN_MARGIN}:
        return (
            "Move with de-vigged team-goal and match-result odds, confirmed attacking lineups and tactical "
            "mismatch; for conjunctions require evidence supporting every leg rather than multiplying nudges."
        )
    if "player" in market:
        return (
            "Adjust primarily for confirmed start, expected minutes, role, position, penalties/set pieces, "
            "recent shot involvement and the player's direct odds; a confirmed bench role should lower sharply."
        )
    if market == TOTAL_SHOTS_THRESHOLD or "shot" in market or "shot" in lower:
        return (
            "Raise with disclosed shot-total conditioning, attacking lineups, territorial dominance and high-tempo "
            "tactics; lower for low possession, missing creators or conservative game plans."
        )
    return (
        "Use exact direct odds first when available; otherwise adjust for related de-vigged odds, confirmed "
        "lineups, injuries, tactics, referee and match-state incentives while preserving the stated time scope."
    )


def build_simulation_report(
    ctx: MatchContext,
    questions: Iterable[str | dict[str, Any]],
    *,
    engine: SimulatorEngine | None = None,
    settings: Settings | None = None,
    market_odds: dict | None = None,
    n_sims: int | None = None,
) -> dict[str, Any]:
    """Price only ``questions`` and return the compact LLM-facing report contract."""
    settings = settings or (engine.settings if engine is not None else default_settings())
    engine = engine or build_engine(settings)
    items = _question_items(questions)
    eligible = [item for item in items if _simulator_can_parse(item, ctx)]
    unsupported = [
        {**item, "reason": "No simulator resolver for this exact question template."}
        for item in items if item not in eligible
    ]
    before_adjust = len(engine.adjust_log)
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
                ctx, [item["question"] for item in eligible], market_odds=market_odds,
                n_sims=effective_sims,
            ))
    except Exception:
        # One unsupported question must not erase the usable evidence for its neighbours.
        predictions = []
        for item in eligible:
            try:
                predictions.append(engine.predict(
                    ctx, item["question"], market_odds=market_odds, n_sims=effective_sims,
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
            "historical_evidence": historical.get(key),
            "conditioning_inputs": {
                "regulation_draw_probability": market_odds.get("regulation_draw_probability"),
                "interpretation": (
                    "Same-book de-vigged regulation draw probability; for match-scope knockout "
                    "contracts, higher values increase expected extra-time exposure."
                ),
            } if market_odds and market_odds.get("regulation_draw_probability") is not None else {},
            "adjustment_guidance": _adjustment_guidance(
                str(pred.market), pred.params or {}, item["question"],
            ),
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
            "odds_anchor_applied": len(engine.adjust_log) > before_adjust,
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
        home,
        away,
        elo_a=float(elo_table.get(canonical_home, 1500.0)),
        elo_b=float(elo_table.get(canonical_away, 1500.0)),
        stage="knockout" if kickoff and knockout_after and kickoff >= knockout_after else "group",
        host_a=canonical_home in hosts,
        host_b=canonical_away in hosts,
        date=kickoff or None,
    )
    ctx.extra["aliases"] = {"A": [canonical_home], "B": [canonical_away]}

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
            lineups.get("A") or lineups.get("home") or lineups.get(home), ctx.team_a,
        )
        ctx.lineup_b = _lineup_players(
            lineups.get("B") or lineups.get("away") or lineups.get(away), ctx.team_b,
        )
    elif isinstance(lineups, list):
        # Native API-Football fixtures/lineups response used by sportspredict-llm.
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
            by_team.get(home) or by_team.get(ctx.team_a) or (ordered[0] if ordered else []), ctx.team_a,
        )
        ctx.lineup_b = _lineup_players(
            by_team.get(away) or by_team.get(ctx.team_b)
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
    """JSON bridge used by ``sportspredict-llm`` and the CLI."""
    settings = settings or default_settings()
    ctx = context_from_payload(payload, settings)
    return build_simulation_report(
        ctx,
        payload.get("questions") or [],
        settings=settings,
        market_odds=payload.get("market_odds") or None,
        n_sims=payload.get("n_sims"),
    )
