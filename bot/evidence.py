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

from . import config, simulator, simulator_benchmark, wc2026_evidence
from . import oddsapi as oapi
from . import predictor as afpred
from .matcher import match_intent, match_intent_oddsapi
from .pricing import PriceCtx


EVIDENCE_DIR = config.ROOT / "logs" / "llm_pricing_runs"
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
        why = (
            "regulation first-team-to-score proxy for the full-match contract; "
            "extra-time-only difference accepted as immaterial"
            if (spec or {}).get("scope_proxy") else "exact mapped contract"
        )
        direct_by_market[mid] = _tag_observations(direct, "direct", why)
        spec_by_market[mid] = spec

    # Direct odds are computed first so the simulator only prices the markets
    # without an exact direct contract (plus the retained model-sensitive
    # penalty/shot-on-target targets). It preserves direct-odds priority: a
    # liquid exact price is never displaced by simulator context.
    simulator_by_market = simulator.simulator_estimates(
        result.markets,
        ctx,
        direct_by_market=direct_by_market,
        intents=result.intents,
        kickoff=result.sp_match.get("opening_time"),
        referee=_fixture_referee(result),
        stage=_fixture_stage(result),
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
        if not direct and mid in simulator_by_market:
            item["simulator_estimate"] = _compact_simulator_estimate(
                simulator_by_market[mid]
            )
        guidance = _player_form_guidance(intent)
        if guidance:
            item["adjustment_guidance"] = guidance
        question_evidence.append(item)

    evidence = {
        "schema_version": 17,
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


def _player_form_guidance(intent: dict | None) -> str | None:
    """Tell the pricing model how to use top-level player form for player markets."""
    player = (intent or {}).get("player")
    if not player or player == "None":
        return None
    market = (intent or {}).get("market")
    if market == "player_goal_scorer":
        metrics = "goals_per90, shots_per90, sot_per90, starts and minutes"
    elif market == "player_shots_on_target":
        metrics = "sot_per90, shots_per90, starts and minutes"
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
    )


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


def _compact_simulator_estimate(estimate: dict) -> dict:
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
    return compact


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
        out[team] = {
            "formation": entry.get("formation"),
            "starting_xi": [name for name in xi if name],
            "bench": [name for name in bench if name],
        }
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
