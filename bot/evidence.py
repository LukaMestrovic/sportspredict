"""Build auditable per-match evidence for the LLM pricing layer.

The evidence file is the deterministic handoff between provider odds and the
raw LLM judgement. It contains per-book de-vigged probabilities for the exact
SportPredict contract, or one simulator fallback when no exact price exists.
Broad related-market bundles are deliberately excluded.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from . import config, public_odds, simulator, simulator_benchmark, wc2026_evidence
from . import oddsapi as oapi
from . import predictor as afpred
from .matcher import (
    CARDS_COMPARE_PROXY,
    CARDS_COMPARE_PROXY_NOTE,
    TEAM_SCORE_NO_OWN_GOALS_PROXY,
    TEAM_SCORE_NO_OWN_GOALS_PROXY_NOTE,
    match_intent,
    match_intent_oddsapi,
)
from .pricing import PriceCtx


EVIDENCE_DIR = config.ROOT / "logs" / "llm_pricing_runs"
MIN_BASELINE_COMPARISON_OBSERVATIONS = 30


def build_match_evidence(
    result,
    ctx: PriceCtx,
    lineups: list[dict] | None,
    minutes_before: float | None,
    af=None,
) -> dict:
    """Return the full JSON-serialisable evidence bundle for one match."""
    direct_by_market: dict[str, list[dict]] = {}
    spec_by_market: dict[str, dict | None] = {}
    for market in result.markets:
        mid = market["id"]
        intent = result.intents.get(mid)
        direct, spec = _direct_odds(intent, ctx)
        why = _direct_contract_note(spec)
        direct_by_market[mid] = _tag_observations(direct, "direct", why)
        spec_by_market[mid] = spec

    # Direct odds are computed first so the simulator only prices the markets
    # without an exact direct contract (plus the retained model-sensitive
    # penalty/shot-on-target targets). It preserves direct-odds priority: a
    # liquid exact price is never displaced by simulator context.
    stage = _fixture_stage(result) or ctx.stage
    simulator_by_market = simulator.simulator_estimates(
        result.markets,
        ctx,
        direct_by_market=direct_by_market,
        intents=result.intents,
        kickoff=result.sp_match.get("opening_time"),
        referee=_fixture_referee(result),
        stage=stage,
        lineups=lineups,
    )
    if simulator_by_market:
        _drop_static_wc2026_scopes(simulator_by_market)
    wc2026_refresh = None
    if simulator_by_market and af is not None:
        try:
            contract_keys = {
                estimate.get("contract_key") for estimate in simulator_by_market.values()
                if estimate.get("contract_key")
            }
            wc2026_refresh = wc2026_evidence.refresh(
                af, result.sp_match.get("opening_time"), contract_keys,
            )
            wc2026_evidence.overlay(simulator_by_market, wc2026_refresh)
        except Exception as exc:
            wc2026_refresh = {
                "complete": False,
                "error": f"WC2026 evidence refresh failed: {exc}",
            }
    live_benchmark = simulator_benchmark.load()
    if simulator_by_market:
        simulator_benchmark.overlay(simulator_by_market, live_benchmark)

    context = getattr(result, "match_context", None) or {}

    question_evidence = []
    for market in result.markets:
        mid = market["id"]
        question = market["question"]
        intent = result.intents.get(mid)
        direct = direct_by_market[mid]
        item = {
            "intent": intent,
            "market_id": mid,
            "question": question,
            "contract_scope": _contract_scope(intent),
            "direct_market_spec": spec_by_market[mid],
        }
        item["direct_odds"] = [_compact_direct_odd(obs) for obs in direct]
        online = [] if direct else public_odds.online_odds(intent, result.home, result.away)
        if online:
            item["online_odds_candidates"] = online
        if not direct and mid in simulator_by_market:
            item["simulator_estimate"] = _compact_simulator_estimate(
                simulator_by_market[mid], stage=stage,
            )
        guidance = _adjustment_guidance(intent, question, result.home, result.away)
        if guidance:
            item["adjustment_guidance"] = guidance
        question_evidence.append(item)

    evidence = {
        "schema_version": 19,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "match": _match_meta(result, lineups, minutes_before),
        "team_form": context.get("team_form") or {},
        "player_form": context.get("player_form") or {},
        "referee_profile": context.get("referee_profile") or {},
        "injuries": context.get("injuries") or {},
        "question_evidence": question_evidence,
    }
    evidence["evidence_hash"] = evidence_hash(evidence)
    return evidence


def _drop_static_wc2026_scopes(estimates: dict[str, dict]) -> None:
    """Keep current-tournament scopes only when a live refresh overlays them."""
    for estimate in estimates.values():
        history = estimate.get("historical_evidence")
        if not isinstance(history, dict):
            continue
        for section in (
            "empirical_rate", "contract_performance",
            "model_performance", "family_performance",
        ):
            rows = history.get(section)
            if not isinstance(rows, dict):
                continue
            rows.pop("wc2026", None)
            rows.pop("wc2026_knockout", None)


def write_evidence(evidence: dict, *, directory: Path = EVIDENCE_DIR) -> Path:
    """Persist the evidence JSON and return its path."""
    directory.mkdir(parents=True, exist_ok=True)
    match = evidence.get("match", {})
    kickoff = str(match.get("kickoff") or "unknown").replace(":", "").replace("-", "")
    slug = _slug(f"{match.get('home') or 'home'}_vs_{match.get('away') or 'away'}")
    path = directory / f"{kickoff}_{slug}_{evidence['evidence_hash'][:10]}_evidence.json"
    path.write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2, sort_keys=False) + "\n"
    )
    return path


def evidence_hash(evidence: dict) -> str:
    """Stable hash over the evidence content, excluding any existing hash field."""
    data = dict(evidence)
    data.pop("evidence_hash", None)
    blob = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _match_meta(result, lineups, minutes_before: float | None) -> dict:
    fixture = (result.fixture or {}).get("fixture", {}) if result.fixture else {}
    venue = fixture.get("venue") or {}
    venue_str = None
    if venue.get("name"):
        venue_str = venue["name"] + (f", {venue['city']}" if venue.get("city") else "")
    return {
        "match_id": result.sp_match["id"],
        "name": result.sp_match.get("name"),
        "home": result.home,
        "away": result.away,
        "kickoff": result.sp_match["opening_time"],
        "minutes_to_kickoff": round(minutes_before, 1) if minutes_before is not None else None,
        "venue": venue_str,
        "referee": fixture.get("referee"),
        "lineups": summarize_lineups(lineups),
    }


def _adjustment_guidance(
    intent: dict | None, question: str, home: str, away: str
) -> str | None:
    """Market-specific research checklist for the pricing model."""
    parts = []
    player_guidance = _player_form_guidance(intent)
    if player_guidance:
        parts.append(player_guidance)
    stat_guidance = _stat_market_guidance(intent, question, home, away)
    if stat_guidance:
        parts.append(stat_guidance)
    compound_guidance = _compound_market_guidance(question)
    if compound_guidance:
        parts.append(compound_guidance)
    special_guidance = _special_market_guidance(question)
    if special_guidance:
        parts.append(special_guidance)
    return "\n\n".join(parts) if parts else None


def _player_form_guidance(intent: dict | None) -> str | None:
    """Tell the pricing model how to use top-level player form for player markets."""
    player = (intent or {}).get("player")
    if not player or player == "None":
        return None
    market = (intent or {}).get("market")
    search_clause = ""
    if market == "player_goal_scorer":
        metrics = "goals_per90, shots_per90, sot_per90, starts and minutes"
    elif market == "player_shots_on_target":
        metrics = "sot_per90, shots_per90, starts and minutes"
        search_clause = (
            " Search online for direct player shots-on-target props, including "
            "bet-builder Player Total Shots On Target / SoT tabs and any half-specific "
            "variant before relying only on form."
        )
    elif market == "player_score_or_assist":
        metrics = "assists, chances created, goals_per90, shots_per90, starts and minutes"
        search_clause = (
            " Search online for direct bookmaker or bet-builder prices labelled "
            "\"player to score or assist\", \"score or assist\", \"to score or assist\", "
            "or player assists plus anytime scorer component prices. Use an exact "
            "score-or-assist quote as direct online odds; if only components exist, "
            "combine them as a labeled proxy and avoid double-counting overlap."
        )
    else:
        metrics = "recent attacking involvement, starts and minutes"
    return (
        f"Use top-level player_form to locate {player} by name. Treat direct_odds "
        "probability_pct values as the bookmaker spread when present; lean toward "
        f"the high end only when the player's {metrics} plus lineup context are "
        "strong for the target role, and lean toward the low end for weak recent involvement, "
        "rotation or bench risk, limited minutes, or role uncertainty. When no "
        "direct_odds exist, use simulator_estimate as context and apply the same "
        "player-form adjustment."
        f"{search_clause}"
    )


def _stat_market_guidance(
    intent: dict | None, question: str, home: str, away: str
) -> str | None:
    """Push the LLM toward exact online stat markets when providers lack them."""
    intent = intent or {}
    market = intent.get("market")
    lower = question.lower()
    if market not in ("team_shots_on_target", "total_shots_on_target") and (
        "shot on target" not in lower and "shots on target" not in lower
    ):
        return None

    side = intent.get("subject")
    period = intent.get("period")
    player = intent.get("player")
    team = home if side == "home" else away if side == "away" else None
    threshold = intent.get("threshold")
    line_hint = None
    try:
        if intent.get("comparator") == "gte" and threshold is not None:
            line_hint = f"{float(threshold) - 0.5:g}"
        elif intent.get("comparator") == "lte" and threshold is not None:
            line_hint = f"{float(threshold) + 0.5:g}"
    except (TypeError, ValueError):
        line_hint = None
    if player:
        target = f"{player} shots on target"
    elif "both teams" in lower:
        target = "both teams shots on target"
    else:
        target = f"{team} shots on target" if team else "match/team shots on target"
    line_clause = f" at line {line_hint}" if line_hint else ""
    period_clause = ""
    if period == "1H":
        period_clause = " for the first half / 1st half tab"
    elif period == "2H":
        period_clause = " for the second half / 2nd half tab"
    return (
        f"Before relying on the simulator for this SOT stat market, search for "
        f"direct online odds for {target}{line_clause}{period_clause}: phrases such as "
        "\"team total shots on target\", \"player total shots on target\", "
        "\"shots on goal\", \"Team Total\", \"alternative team total\", "
        "\"most shots on target\", and \"statistics\" pages. On bookmaker "
        "statistics pages, if the enclosing event is labelled shots on target, "
        "generic row headers like Total, Total Goals, or Team Total refer to the "
        "stat count, not football goals. For WC2026, explicitly check BetOlimp "
        "World Cup 2026 Statistics pages whose titles look like "
        f"\"{home} (shots on target) - {away} (shots on target)\". "
        "Use an exact over/under pair when found "
        "and de-vig the two sides from the same book; use 1x2/most-SOT markets "
        "as strong proxies for comparison questions when the period and statistic match."
    )


def _compound_market_guidance(question: str) -> str | None:
    lower = question.lower()
    if "penalty kick" in lower and "red card" in lower:
        return (
            "For this penalty/red-card OR compound, first search for an exact combined "
            "special such as \"Penalty or Red card: yes\" / \"Penalty or Red Card\". "
            "If absent, search for direct component odds for penalty awarded and red "
            "card shown before relying on the simulator. Use same-source prices when "
            "possible, convert each component probability, combine as a union, and "
            "note any positive correlation adjustment."
        )
    if (
        "both teams score" in lower
        and ("3 or more total goals" in lower or "over 2.5" in lower)
    ):
        return (
            "For this BTTS plus goals compound, search for exact combined odds such "
            "as \"Both Teams To Score and Over 2.5 Goals\", \"BTTS & Over 2.5\", "
            "or bet-builder same-game combinations. Treat exact combined prices as "
            "direct online odds; if absent, use the provided component/derived "
            "context and explain the positive correlation."
        )
    return None


def _special_market_guidance(question: str) -> str | None:
    """Research checklist for online match-special markets not in provider feeds."""
    lower = question.lower()
    parts: list[str] = []
    if "hydration break" in lower:
        parts.append(
            "Search bookmaker Match Specials / Market Specials for hydration-break "
            "props. Exact phrases include \"Goal scored before the 1st half hydration "
            "break\" and \"goal before first hydration break\". For after-second-"
            "hydration goal questions, exact after-break props are best; otherwise "
            "nearby late-window prices such as \"Goal scored 80:00 - Full time\" or "
            "\"Goal scored 85:00 - Full time\" are only labeled proxies. For card, "
            "corner, offside, or substitution hydration questions, use a special only "
            "when both the event type and time window match closely."
        )
    if "substitute" in lower and ("score" in lower or "goal" in lower):
        parts.append(
            "Search match specials for \"A substitute to score\", \"Substitute to "
            "come on and score\", and \"Bench player will score\". Treat exact "
            "substitute-to-score prices as direct online odds for this contract, "
            "with an own-goal caveat when the SportPredict wording excludes own goals."
        )
    if "stoppage" in lower or "added time" in lower:
        parts.append(
            "Search for exact stoppage/added-time goal specials first. If unavailable, "
            "late-window prices such as \"45:00 - half time\", \"80:00 - Full time\", "
            "or \"85:00 - Full time\" can be directional proxies only when their "
            "time window is clearly broader than the SportPredict contract."
        )
    if "outside the penalty area" in lower or "outside the box" in lower:
        parts.append(
            "Search scoring-event and goal-method specials for \"outside the box\", "
            "\"outside the penalty area\", and \"method of goal\" before falling back "
            "to simulator/base-rate evidence."
        )
    if "any player" in lower and (
        "more than 1 goal" in lower or "2 or more shots on target" in lower
    ):
        parts.append(
            "Search match specials and player-prop tabs for \"any player to score 2+\", "
            "\"to score a brace\", \"any player 2+ shots on target\", and player "
            "ladder markets. Use exact any-player prices when found; otherwise combine "
            "top-player ladders only as a labeled proxy."
        )
    if "substitution" in lower and "before halftime" in lower:
        parts.append(
            "Search match specials for first-substitution timing / substitution before "
            "halftime. If no exact timing market is found, do not treat generic "
            "substitution or lineup news as direct odds."
        )
    return "\n\n".join(parts) if parts else None


def _contract_scope(intent: dict | None) -> dict:
    """Plain-language settlement scope for the pricing-model handoff."""
    intent = intent or {}
    time_scope = intent.get("time_scope") or "unknown"
    if intent.get("market") == "to_advance":
        interpretation = "Qualification outcome after extra time and penalties if required."
    elif time_scope == "regulation":
        interpretation = "Regulation only: 90 minutes plus stoppage time; exclude extra time."
    elif time_scope == "full_match":
        interpretation = "Full match: include extra time if played; exclude shootout events."
    else:
        interpretation = "Scope was not resolved; do not assume regulation odds are exact."
    return {"time_scope": time_scope, "interpretation": interpretation}


def _compact_direct_odd(observation: dict) -> dict:
    """Keep only the de-vigged price, coherent contract, and audit provenance."""
    compact = {
        key: observation.get(key)
        for key in (
            "source", "bookmaker", "market_key", "contract",
            "probability_pct", "devig_method",
        )
        if observation.get(key) is not None
    }
    why = observation.get("why_relevant")
    if why and why != "exact mapped contract":
        compact["contract_note"] = why
    return compact


def _compact_simulator_estimate(estimate: dict, *, stage: str | None = None) -> dict:
    """Project the verbose internal simulator report into LLM decision inputs."""
    probability_pct = estimate.get("probability_pct")
    if probability_pct is None and estimate.get("probability") is not None:
        probability_pct = round(float(estimate["probability"]) * 100.0, 2)
    compact = {
        key: value for key, value in (
            ("contract_key", estimate.get("contract_key")),
            ("probability_pct", probability_pct),
            ("basis", estimate.get("explanation")),
            ("adjustment_guidance", estimate.get("adjustment_guidance")),
        ) if value is not None
    }

    inputs = estimate.get("conditioning_inputs") or {}
    draw = inputs.get("regulation_draw_probability")
    if draw is not None:
        compact["conditioning"] = {
            "regulation_draw_probability_pct": round(float(draw) * 100.0, 2),
        }

    history = estimate.get("historical_evidence") or {}
    empirical_rates = {}
    empirical_source = history.get("empirical_rate") or {}
    for scope in ("all_history", "all_history_knockout", "wc2026", "wc2026_knockout"):
        if not empirical_source:
            break
        row = empirical_source.get(scope)
        if not isinstance(row, dict):
            continue
        if not row.get("available") or row.get("rate") is None:
            empirical_rates[scope] = {
                "n": 0,
                "rate": None,
                "population": _population_description(scope, row),
            }
            continue
        observations = row.get("observations") or row.get("matches")
        rate = {
            "rate": round(float(row["rate"]), 6),
            "population": _population_description(scope, row),
        }
        if observations is not None:
            rate["n"] = int(observations)
        empirical_rates[scope] = rate
    if empirical_rates:
        compact["empirical_rates"] = empirical_rates

    def compact_contract_comparisons(source: dict) -> dict:
        comparisons = {}
        if not source:
            return comparisons
        for scope in ("all_history", "all_history_knockout", "wc2026", "wc2026_knockout"):
            row = source.get(scope)
            if not isinstance(row, dict):
                continue
            if not row.get("available"):
                comparisons[scope] = {
                    "basis": _comparison_basis_description(scope),
                    "brier": None,
                    "n_observations": 0,
                    "signal": "unavailable_no_observations",
                }
                continue
            comparison = {
                "basis": _comparison_basis_description(scope),
                "signal": row.get("comparison_signal"),
                "n_observations": (
                    row.get("observations")
                    or (row.get("coverage") or {}).get("comparable_observations")
                    or row.get("questions")
                    or row.get("matches")
                ),
            }
            brier = row.get("brier") or {}
            comparison["brier"] = {
                key: brier.get(key)
                for key in ("simulator", "empirical_rate", "always_50")
                if brier.get(key) is not None
            }
            comparisons[scope] = {
                key: value for key, value in comparison.items() if value is not None
            }
        return comparisons

    contract_comparisons = compact_contract_comparisons(
        history.get("contract_performance") or {},
    )
    if contract_comparisons:
        compact["contract_comparison"] = contract_comparisons
    calibrated = _calibrated_baseline(compact, stage=stage)
    if calibrated:
        compact["calibrated_baseline"] = calibrated
    return compact


def _calibrated_baseline(compact: dict, *, stage: str | None = None) -> dict | None:
    """Select the no-direct-odds baseline from exact-contract calibration."""
    comparisons = compact.get("contract_comparison") or {}
    if not isinstance(comparisons, dict) or not comparisons:
        return None

    skipped_small_sample = False
    for scope in _baseline_scope_order(stage):
        row = comparisons.get(scope)
        if not isinstance(row, dict):
            continue
        n_observations = _safe_int(row.get("n_observations"))
        if (
            n_observations is not None
            and n_observations < MIN_BASELINE_COMPARISON_OBSERVATIONS
        ):
            skipped_small_sample = True
            continue
        candidates = _baseline_candidates(compact, row, scope)
        if not candidates:
            continue
        best = min(candidates, key=lambda candidate: candidate["brier"])
        return _baseline_payload(
            best, compact, row, scope, n_observations,
            skipped_small_sample=skipped_small_sample,
        )

    probability_pct = _safe_float(compact.get("probability_pct"))
    if probability_pct is None:
        return None
    reason = (
        "No exact-contract calibration scope had enough observations; start from "
        "the simulator fallback and keep empirical snippets as small-sample context."
    )
    return {
        "source": "simulator",
        "probability_pct": round(probability_pct, 2),
        "reason": reason,
    }


def _baseline_scope_order(stage: str | None) -> tuple[str, ...]:
    if stage == "knockout":
        return ("wc2026_knockout", "all_history_knockout", "wc2026", "all_history")
    return ("wc2026", "all_history", "wc2026_knockout", "all_history_knockout")


def _baseline_candidates(compact: dict, comparison: dict, scope: str) -> list[dict]:
    brier = comparison.get("brier") or {}
    if not isinstance(brier, dict):
        return []
    candidates = []
    simulator_probability = _safe_float(compact.get("probability_pct"))
    simulator_brier = _safe_float(brier.get("simulator"))
    if simulator_probability is not None and simulator_brier is not None:
        candidates.append({
            "source": "simulator",
            "probability_pct": simulator_probability,
            "brier": simulator_brier,
        })

    empirical_row = (compact.get("empirical_rates") or {}).get(scope) or {}
    empirical_rate = _safe_float(empirical_row.get("rate"))
    empirical_brier = _safe_float(brier.get("empirical_rate"))
    if empirical_rate is not None and empirical_brier is not None:
        candidates.append({
            "source": "empirical_rate",
            "probability_pct": empirical_rate * 100.0,
            "brier": empirical_brier,
            "rate_n": _safe_int(empirical_row.get("n")),
            "population": empirical_row.get("population"),
        })

    fifty_brier = _safe_float(brier.get("always_50"))
    if fifty_brier is not None:
        candidates.append({
            "source": "always_50",
            "probability_pct": 50.0,
            "brier": fifty_brier,
        })
    return candidates


def _baseline_payload(
    selected: dict,
    compact: dict,
    comparison: dict,
    scope: str,
    n_observations: int | None,
    *,
    skipped_small_sample: bool,
) -> dict:
    brier = {
        key: value
        for key in ("simulator", "empirical_rate", "always_50")
        if (value := _safe_float((comparison.get("brier") or {}).get(key))) is not None
    }
    payload = {
        "source": selected["source"],
        "probability_pct": round(float(selected["probability_pct"]), 2),
        "scope": scope,
        "comparison_n": n_observations,
        "signal": comparison.get("signal"),
        "brier": brier,
        "reason": _baseline_reason(selected["source"], scope, brier, skipped_small_sample),
    }
    if selected.get("rate_n") is not None:
        payload["rate_n"] = selected["rate_n"]
    if selected.get("population"):
        payload["population"] = selected["population"]
    simulator_probability = _safe_float(compact.get("probability_pct"))
    if simulator_probability is not None and selected["source"] != "simulator":
        payload["simulator_probability_pct"] = round(simulator_probability, 2)
    return {key: value for key, value in payload.items() if value is not None}


def _baseline_reason(
    source: str,
    scope: str,
    brier: dict,
    skipped_small_sample: bool,
) -> str:
    labels = {
        "simulator": "simulator",
        "empirical_rate": "empirical rate",
        "always_50": "50/50 baseline",
    }
    pieces = [
        f"Exact-contract {scope} calibration has the lowest Brier for "
        f"{labels.get(source, source)}."
    ]
    if skipped_small_sample:
        pieces.append("Smaller current-tournament scope was ignored by sample-size guard.")
    if brier:
        bits = ", ".join(
            f"{key}={value}" for key, value in brier.items()
        )
        pieces.append(f"Brier comparison: {bits}.")
    return " ".join(pieces)


def _safe_float(value) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _safe_int(value) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _population_description(scope: str, row: dict) -> str:
    if scope == "all_history":
        return "All historical labelable observations for this exact contract."
    if scope == "all_history_knockout":
        return (
            "Historical labelable observations for this exact contract, restricted "
            "to knockout-stage matches."
        )
    if scope == "wc2026":
        return (
            "Settled WC2026 labelable observations for this exact contract before "
            "the target kickoff."
        )
    if scope == "wc2026_knockout":
        return (
            "Settled WC2026 knockout-stage labelable observations for this exact "
            "contract before the target kickoff."
        )
    return str(row.get("population") or "Labelable observations for this exact contract.")


def _comparison_basis_description(scope: str) -> str:
    if scope == "all_history":
        return "Rolling-origin unseen historical observations for this exact contract."
    if scope == "all_history_knockout":
        return (
            "Rolling-origin unseen historical knockout-stage observations for this "
            "exact contract."
        )
    if scope == "wc2026":
        return (
            "Frozen pre-2026 simulator on every settled WC2026 labelable observation "
            "for this exact contract."
        )
    if scope == "wc2026_knockout":
        return (
            "Frozen pre-2026 simulator on every settled WC2026 knockout-stage "
            "labelable observation for this exact contract."
        )
    return "Comparable unseen observations for this exact contract."


def _fixture_referee(result) -> str | None:
    fixture = (result.fixture or {}).get("fixture", {}) if result.fixture else {}
    return fixture.get("referee")


def _fixture_stage(result) -> str | None:
    """Map the API-Football round to the simulator's stage when unambiguous.

    Returns ``"group"``/``"knockout"`` from the league round (e.g. "Group Stage
    - 1", "Round of 16"), else ``None`` so the simulator derives it from kickoff.
    """
    league = (result.fixture or {}).get("league", {}) if result.fixture else {}
    rnd = str(league.get("round") or "").lower()
    if not rnd:
        return None
    if "group" in rnd:
        return "group"
    if any(key in rnd for key in ("round of", "16", "8", "quarter", "semi", "final")):
        return "knockout"
    return None


def summarize_lineups(lineups: list[dict] | None) -> dict | None:
    if not lineups:
        return None
    out: dict[str, dict] = {}
    for entry in lineups:
        team = (entry.get("team") or {}).get("name") or "?"
        xi = [(pl.get("player") or {}).get("name") for pl in entry.get("startXI", [])]
        bench = [(pl.get("player") or {}).get("name")
                 for pl in entry.get("substitutes", [])]
        summary = {
            "formation": entry.get("formation"),
            "starting_xi": [name for name in xi if name],
            "bench": [name for name in bench if name],
        }
        if entry.get("source"):
            summary["source"] = entry["source"]
        out[team] = summary
    return out


def _direct_odds(intent: dict | None, ctx: PriceCtx) -> tuple[list[dict], dict | None]:
    if not intent:
        return [], None
    af_spec = match_intent(intent, ctx.home, ctx.away, stage=ctx.stage)
    oa_spec = (
        match_intent_oddsapi(intent, ctx.home, ctx.away, stage=ctx.stage)
        if ctx.oa else None
    )
    obs = []
    if af_spec:
        obs.extend(afpred.observations(ctx.af_books, af_spec))
    if oa_spec and ctx.oa and ctx.oa_event:
        books = ctx.oa.event_odds(ctx.oa_event["id"], [oa_spec["market"]])
        obs.extend(oapi.observations(books, oa_spec))
    return obs, af_spec or oa_spec


def _direct_contract_note(spec: dict | None) -> str:
    """Explain why a direct-odds contract should be used for the question."""
    if not spec:
        return "exact mapped contract"
    if spec.get("scope_proxy"):
        return (
            "regulation first-team-to-score proxy for the full-match contract; "
            "extra-time-only difference accepted as immaterial"
        )
    if spec.get("contract_proxy") == CARDS_COMPARE_PROXY:
        return CARDS_COMPARE_PROXY_NOTE
    if spec.get("contract_proxy") == TEAM_SCORE_NO_OWN_GOALS_PROXY:
        return TEAM_SCORE_NO_OWN_GOALS_PROXY_NOTE
    if spec.get("proxy_note"):
        return str(spec["proxy_note"])
    return "exact mapped contract"


def _tag_observations(observations: Iterable[dict], role: str, why: str) -> list[dict]:
    tagged = []
    for obs in observations:
        item = dict(obs)
        item["role"] = role
        item["why_relevant"] = why
        tagged.append(item)
    return tagged


def _slug(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "_", value).strip("_").lower()
    return slug[:80] or "match"
