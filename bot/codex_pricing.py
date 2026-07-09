"""Validate and render audited Codex pricing responses.

Codex performs research outside this process.  This module is deliberately a
pure local boundary: it parses the returned JSON, validates every market and
public audit field, attaches submission objects, and writes retained reports.
It contains no model client, network call, or model configuration.
"""
from __future__ import annotations

import json
import math
import re
from datetime import datetime, timezone
from pathlib import Path

from . import config
from .pipeline import Prediction


CODEX_RESPONSE_SCHEMA_VERSION = 1
PROMPT_PATH = config.ROOT / "prompts" / "codex_pricing_prompt.md"

REQUIRED_AUDIT_FIELDS = (
    "provided_odds_used",
    "online_odds_found",
    "non_odds_factors_used",
    "ignored_or_downweighted_evidence",
    "reasoning_summary",
    "sources",
)
MATCH_READ_ASPECTS = (
    "tactics_tempo_game_state",
    "lineups_minutes_availability",
    "attacking_defensive_profile",
    "stat_market_shape",
    "set_pieces_goal_methods",
    "referee_cards_penalties",
    "venue_weather_rest_motivation",
    "broad_market_consensus",
)

_prompt_cache: str | None = None


def _load_prompt() -> str:
    global _prompt_cache
    if _prompt_cache is None:
        _prompt_cache = PROMPT_PATH.read_text(encoding="utf-8")
    return _prompt_cache


def _extract_json(text: str) -> dict:
    text = text.strip()
    try:
        value = json.loads(text)
    except ValueError:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if not match:
            raise
        value = json.loads(match.group(0))
    if not isinstance(value, dict):
        raise ValueError("Codex pricing response must be a JSON object")
    return value


def apply_pricing_response(
    result,
    evidence: dict,
    evidence_path: Path | None,
    response: dict,
    *,
    require_all_markets: bool = False,
    model_label: str | None = None,
    expected_session_id: str | None = None,
    expected_evidence_hash: str | None = None,
    directory: Path | None = None,
):
    """Validate an audited pricing JSON response and attach predictions."""
    binding_error = _binding_error(
        response,
        expected_session_id=expected_session_id,
        expected_evidence_hash=expected_evidence_hash,
    )
    if binding_error:
        _skip_all(result, binding_error)
        if require_all_markets:
            raise ValueError(binding_error)
        return result
    match_read = str(response.get("match_read_markdown") or "").strip()
    if not match_read:
        _skip_all(result, "Codex pricing missing match_read_markdown")
        if require_all_markets:
            raise ValueError("Codex pricing missing match_read_markdown")
        return result

    markets = response.get("markets")
    if not isinstance(markets, list):
        _skip_all(result, "Codex pricing returned no markets list")
        if require_all_markets:
            raise ValueError("Codex pricing returned no markets list")
        return result
    ok, reason = validate_response_audit(response, evidence, markets)
    if not ok:
        _skip_all(result, reason)
        if require_all_markets:
            raise ValueError(reason)
        return result

    result.codex_briefing = response.get("briefing")
    result.codex_sources = response.get("sources") or []
    result.codex_response = response

    by_market = {
        item.get("market_id"): item for item in markets
        if isinstance(item, dict) and item.get("market_id") is not None
    }
    evidence_by_market = {
        item["market_id"]: item for item in evidence.get("question_evidence", [])
    }
    result.predictions = []
    result.skipped = []
    result.skip_reasons = {}
    for market in result.markets:
        mid = market["id"]
        audit = by_market.get(mid)
        if not audit:
            _skip(result, market, "Codex pricing omitted market audit")
            continue
        q_evidence = evidence_by_market.get(mid, {})
        validated = validate_market_audit(audit, q_evidence)
        if not validated[0]:
            _skip(result, market, validated[1])
            continue
        probability_int = validated[2]
        direct = q_evidence.get("direct_odds") or []
        pred = Prediction(
            market_id=mid,
            question=market["question"],
            probability=probability_int / 100.0,
            probability_int=probability_int,
            n_books=len(direct),
            market_label="Codex audited price",
            source=model_label or "manual-codex",
            book_probabilities=[obs["probability"] for obs in direct
                                if isinstance(obs.get("probability"), (int, float))],
        )
        pred.codex_audit = audit
        pred.codex_sources = audit.get("sources") or []
        pred.codex_reasoning_summary = audit.get("reasoning_summary")
        result.predictions.append(pred)

    if require_all_markets and result.skipped:
        problems = "; ".join(f"{q}: {why}" for q, why in result.skipped[:5])
        raise ValueError(f"invalid pricing response: {problems}")

    audit_path, report_path, match_read_path = write_audit_bundle(
        result, evidence, evidence_path, response, model_label=model_label,
        directory=directory,
    )
    result.codex_audit_path = str(audit_path)
    result.codex_report_path = str(report_path)
    result.codex_match_read_path = str(match_read_path)
    return result


def _binding_error(
    response: dict,
    *,
    expected_session_id: str | None,
    expected_evidence_hash: str | None,
) -> str | None:
    """Return a fail-closed error when a response is bound to another run."""
    if not isinstance(response, dict):
        return "Codex pricing response must be a JSON object"
    if response.get("schema_version") != CODEX_RESPONSE_SCHEMA_VERSION:
        return "Codex response schema_version is missing or unsupported"
    if expected_session_id is not None:
        if response.get("session_id") != expected_session_id:
            return "Codex response session_id does not match the prepared session"
    if expected_evidence_hash is not None:
        if response.get("evidence_hash") != expected_evidence_hash:
            return "Codex response evidence_hash does not match the prepared evidence"
    return None


def validate_response_audit(
    response: dict, evidence: dict, markets: list[dict]
) -> tuple[bool, str]:
    """Validate top-level public audit scaffolding."""
    if not str(response.get("briefing") or "").strip():
        return False, "Codex pricing briefing must be non-empty"
    for field in ("sources", "match_read_sources"):
        value = response.get(field)
        if not _valid_sources(value):
            return False, f"Codex pricing {field} must be a non-empty list"
    expected = _expected_market_keys(evidence)
    ids = []
    question_ids = {
        item.get("market_id"): item.get("question_id")
        for item in evidence.get("question_evidence", [])
        if isinstance(item, dict) and item.get("market_id") is not None
    }
    for index, market in enumerate(markets):
        if not isinstance(market, dict) or market.get("market_id") is None:
            return False, f"Codex pricing markets[{index}] is missing market_id"
        mid = market["market_id"]
        if isinstance(mid, bool) or not isinstance(mid, (str, int)):
            return False, f"Codex pricing markets[{index}] has invalid market_id"
        ids.append(mid)
        expected_question_id = question_ids.get(mid)
        if expected_question_id and market.get("question_id") != expected_question_id:
            return False, f"Codex pricing market {mid} has wrong question_id"
    if len(ids) != len(set(ids)):
        return False, "Codex pricing markets contains duplicate market_id"
    if set(ids) != expected:
        return False, "Codex pricing markets must exactly match evidence markets"
    return _validate_subagent_memos(response.get("subagent_memos"), evidence, markets)


def _validate_subagent_memos(
    memos, evidence: dict, markets: list[dict]
) -> tuple[bool, str]:
    if not isinstance(memos, dict):
        return False, "Codex pricing missing subagent_memos object"
    expected_sections = {"base_pricing", "match_read_aspects", "question_adjustments"}
    if set(memos) != expected_sections:
        return False, "Codex pricing subagent_memos sections are incomplete or unknown"
    expected = _expected_market_keys(evidence)
    expected_question_ids = {
        item.get("market_id"): item.get("question_id")
        for item in evidence.get("question_evidence", [])
        if isinstance(item, dict) and item.get("market_id") is not None
    }
    market_audits = {
        item.get("market_id"): item for item in markets
        if isinstance(item, dict) and item.get("market_id") is not None
    }
    for section in ("base_pricing", "question_adjustments"):
        rows = memos.get(section)
        if not isinstance(rows, list):
            return False, f"Codex pricing subagent_memos.{section} must be a list"
        seen = set()
        for row in rows:
            if not isinstance(row, dict):
                return False, f"Codex pricing subagent_memos.{section} contains non-object memo"
            mid = row.get("market_id")
            if mid is None:
                return False, f"Codex pricing subagent_memos.{section} missing market_id"
            if mid in seen:
                return False, f"Codex pricing subagent_memos.{section} has duplicate market_id"
            seen.add(mid)
            audit = market_audits.get(mid)
            if not audit:
                return False, f"Codex pricing subagent_memos.{section} references unknown market"
            if section == "base_pricing":
                value = row.get("base_probability_int")
                field = "base_probability_int"
                expected_value = audit.get("base_probability_int")
            else:
                value = row.get("recommended_probability_int")
                field = "recommended_probability_int"
                expected_value = audit.get("probability_int")
            ok, reason, parsed = _probability_int(value, field)
            if not ok:
                return False, reason
            ok, reason, expected_parsed = _probability_int(expected_value, field)
            if not ok:
                return False, reason
            if parsed != expected_parsed:
                return False, (
                    f"Codex pricing subagent_memos.{section} {field} "
                    "does not match market audit"
                )
            if not str(row.get("memo") or row.get("summary") or "").strip():
                return False, f"Codex pricing subagent_memos.{section} missing public memo"
            if not _valid_sources(row.get("sources")):
                return False, f"Codex pricing subagent_memos.{section} missing sources"
            expected_question_id = expected_question_ids.get(mid)
            if expected_question_id and row.get("question_id") != expected_question_id:
                return False, f"Codex pricing subagent_memos.{section} has wrong question_id"
            if section == "base_pricing" and not str(row.get("method") or "").strip():
                return False, "Codex pricing subagent_memos.base_pricing missing method"
        if seen != expected:
            return False, (
                f"Codex pricing subagent_memos.{section} must exactly match evidence markets"
            )

    aspects = memos.get("match_read_aspects")
    if not isinstance(aspects, list):
        return False, "Codex pricing subagent_memos.match_read_aspects must be a list"
    expected_aspects = _expected_aspects(evidence)
    seen_aspects = set()
    for row in aspects:
        if not isinstance(row, dict):
            return False, "Codex pricing subagent_memos.match_read_aspects contains non-object memo"
        aspect = row.get("aspect")
        if aspect in seen_aspects:
            return False, "Codex pricing subagent_memos.match_read_aspects has duplicate aspect"
        if aspect:
            seen_aspects.add(aspect)
        if not str(row.get("memo") or row.get("summary") or "").strip():
            return False, "Codex pricing subagent_memos.match_read_aspects missing public memo"
        sources = row.get("sources")
        if not _valid_sources(sources):
            return False, "Codex pricing subagent_memos.match_read_aspects missing sources"
    if seen_aspects != expected_aspects:
        return False, (
            "Codex pricing subagent_memos.match_read_aspects must exactly match expected aspects"
        )
    return True, ""


def _expected_market_keys(evidence: dict) -> set:
    return {
        item["market_id"] for item in evidence.get("question_evidence", [])
        if isinstance(item, dict) and item.get("market_id") is not None
    }


def _expected_aspects(evidence: dict) -> set:
    workflow = evidence.get("agent_workflow") or {}
    aspects = workflow.get("match_read_aspect_subagents") or MATCH_READ_ASPECTS
    return {str(aspect) for aspect in aspects if aspect}


def _valid_sources(value) -> bool:
    return (
        isinstance(value, list)
        and bool(value)
        and all(isinstance(item, str) and item.strip() for item in value)
    )


def validate_market_audit(
    audit: dict, question_evidence: dict | None = None
) -> tuple[bool, str, int]:
    ok, reason, probability_int = _probability_int(
        audit.get("probability_int"), "probability_int",
    )
    if not ok:
        return False, reason, 0
    ok, reason, base_probability_int = _probability_int(
        audit.get("base_probability_int"), "base_probability_int",
    )
    if not ok:
        return False, reason, 0
    for field in REQUIRED_AUDIT_FIELDS:
        value = audit.get(field)
        if value is None:
            return False, f"Codex pricing missing audit field: {field}", 0
        if field == "reasoning_summary" and not str(value).strip():
            return False, "Codex pricing missing reasoning summary", 0
        if field != "reasoning_summary" and not isinstance(value, list):
            return False, f"Codex pricing audit field must be a list: {field}", 0
        if field == "sources" and not _valid_sources(value):
            return False, "Codex pricing sources must be a non-empty list", 0
    ok, reason = _validate_language_adjustment(
        audit, probability_int, base_probability_int, question_evidence or {},
    )
    if not ok:
        return False, reason, 0
    direct = (question_evidence or {}).get("direct_odds") or []
    if direct and not audit.get("provided_odds_used"):
        ignored_text = json.dumps(
            audit.get("ignored_or_downweighted_evidence") or [],
            ensure_ascii=False,
        ).lower()
        direct_tokens = {
            str(value).lower()
            for observation in direct if isinstance(observation, dict)
            for value in (
                observation.get("bookmaker"), observation.get("contract"),
                observation.get("market_key"),
            )
            if value
        }
        explicitly_rejected = (
            "provided direct odd" in ignored_text
            or "direct odd" in ignored_text
            or any(token in ignored_text for token in direct_tokens)
        )
        if not explicitly_rejected:
            return (
                False,
                "Codex pricing ignored provided direct odds without an explicit audit reason",
                0,
            )
    candidates = (question_evidence or {}).get("online_odds_candidates") or []
    if candidates and not _audit_uses_all_online_candidates(audit, candidates):
        return (
            False,
            "Codex pricing ignored one or more pre-collected online odds candidates",
            0,
        )
    return True, "", probability_int


def _probability_int(value, field: str) -> tuple[bool, str, int]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False, f"Codex pricing missing numeric {field}", 0
    numeric = float(value)
    if not math.isfinite(numeric):
        return False, f"Codex pricing {field} must be finite", 0
    rounded = round(numeric)
    if abs(numeric - rounded) > 1e-9:
        return False, f"Codex pricing {field} must be an integer", 0
    if rounded < 1 or rounded > 99:
        return False, f"Codex pricing {field} outside 1-99", 0
    return True, "", int(rounded)


def _validate_language_adjustment(
    audit: dict,
    probability_int: int,
    base_probability_int: int,
    question_evidence: dict,
) -> tuple[bool, str]:
    adjustment = audit.get("language_adjustment")
    if not isinstance(adjustment, dict):
        return False, "Codex pricing missing language_adjustment object"
    required = (
        "action", "direction", "move_points", "confidence", "base_used",
        "match_read_evidence", "additional_research", "why_move_or_hold",
    )
    for field in required:
        if field not in adjustment:
            return False, f"Codex pricing language_adjustment missing field: {field}"

    ok, reason, base_used = _probability_int(adjustment.get("base_used"), "base_used")
    if not ok:
        return False, reason
    if base_used != base_probability_int:
        return False, "Codex pricing language_adjustment base_used != base_probability_int"

    move_raw = adjustment.get("move_points")
    if isinstance(move_raw, bool) or not isinstance(move_raw, (int, float)):
        return False, "Codex pricing language_adjustment missing numeric move_points"
    move_numeric = float(move_raw)
    if not math.isfinite(move_numeric):
        return False, "Codex pricing language_adjustment move_points must be finite"
    move_points = round(move_numeric)
    if abs(move_numeric - move_points) > 1e-9 or move_points < 0:
        return False, "Codex pricing language_adjustment move_points must be a non-negative integer"

    action = str(adjustment.get("action") or "").lower().strip()
    direction = str(adjustment.get("direction") or "").lower().strip()
    if action not in {"hold", "move"}:
        return False, "Codex pricing language_adjustment action must be hold or move"
    if direction not in {"none", "up", "down"}:
        return False, "Codex pricing language_adjustment direction must be none, up, or down"
    confidence = str(adjustment.get("confidence") or "").lower().strip()
    if confidence not in {"low", "medium", "high"}:
        return False, "Codex pricing language_adjustment confidence must be low, medium, or high"

    if direction == "up":
        expected = base_probability_int + move_points
    elif direction == "down":
        expected = base_probability_int - move_points
    else:
        expected = base_probability_int
    if expected != probability_int:
        return False, "Codex pricing language_adjustment move does not match final probability"

    if move_points == 0:
        if action != "hold" or direction != "none":
            return False, "Codex pricing zero move must use action=hold and direction=none"
    else:
        if action != "move" or direction == "none":
            return False, "Codex pricing non-zero move must use action=move and direction up/down"
        if not str(adjustment.get("why_move_or_hold") or "").strip():
            return False, "Codex pricing non-zero move missing why_move_or_hold"
        evidence_items = adjustment.get("match_read_evidence")
        if not isinstance(evidence_items, list) or not evidence_items:
            return False, "Codex pricing non-zero move missing match_read_evidence"

    if not str(adjustment.get("why_move_or_hold") or "").strip():
        return False, "Codex pricing language_adjustment missing why_move_or_hold"
    if not isinstance(adjustment.get("match_read_evidence"), list):
        return False, "Codex pricing language_adjustment match_read_evidence must be a list"
    if not isinstance(adjustment.get("additional_research"), list):
        return False, "Codex pricing language_adjustment additional_research must be a list"

    cap = _movement_cap(question_evidence)
    if move_points > cap:
        return False, f"Codex pricing language_adjustment move exceeds {cap} point cap"
    return True, ""


def _movement_cap(question_evidence: dict) -> int:
    primary = ((question_evidence.get("decision_basis") or {}).get("primary") or "")
    if primary == "provided_direct_odds" or question_evidence.get("direct_odds"):
        return 5
    if primary == "pre_collected_online_odds" or question_evidence.get("online_odds_candidates"):
        return 6
    return 10


def _audit_uses_all_online_candidates(audit: dict, candidates: list[dict]) -> bool:
    online = audit.get("online_odds_found") or []
    if not online:
        return False
    text = json.dumps(online, ensure_ascii=False).lower()
    for candidate in candidates:
        bookmaker = str(candidate.get("bookmaker") or "").lower()
        url = str(candidate.get("url") or "").lower()
        contract = str(candidate.get("contract") or "").lower()
        tokens = [token for token in (bookmaker, url, contract) if token]
        if not tokens or not any(token in text for token in tokens):
            return False
    return True


def write_audit_bundle(
    result,
    evidence: dict,
    evidence_path: Path | None,
    response: dict,
    *,
    directory: Path | None = None,
    model_label: str | None = None,
) -> tuple[Path, Path, Path]:
    explicit_directory = directory is not None
    directory = directory or config.ROOT / "logs" / "codex_runs"
    directory.mkdir(parents=True, exist_ok=True)
    match = evidence.get("match", {})
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    slug = _slug(f"{match.get('home') or 'home'}_vs_{match.get('away') or 'away'}")
    prefix = "" if explicit_directory else f"{stamp}_{slug}_"
    audit_path = directory / f"{prefix}audit.json"
    report_path = directory / f"{prefix}audit.md"
    match_read_path = directory / f"{prefix}match_read.md"

    model = model_label or "manual-codex"
    match_read_text = str(response.get("match_read_markdown") or "").strip()
    match_read_path.write_text(match_read_text + "\n")
    result.codex_match_read_path = str(match_read_path)
    payload = {
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "analyst": model,
        "evidence_path": str(evidence_path) if evidence_path else None,
        "evidence_hash": evidence.get("evidence_hash"),
        "match_read_path": str(match_read_path),
        "match_read_sources": response.get("match_read_sources") or [],
        "response": response,
        "predictions": [p.codex_audit for p in result.predictions],
        "skipped": [{"question": q, "why": why} for q, why in result.skipped],
    }
    audit_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    report_path.write_text(
        _markdown_report(result, evidence, evidence_path, response, model_label=model)
        + "\n"
    )
    return audit_path, report_path, match_read_path


def _markdown_report(
    result,
    evidence: dict,
    evidence_path: Path | None,
    response: dict,
    *,
    model_label: str | None = None,
) -> str:
    match = evidence.get("match", {})
    lines = [
        f"# Codex pricing audit: {match.get('home')} vs {match.get('away')}",
        "",
        f"- kickoff: {match.get('kickoff')}",
        f"- analyst: {model_label or 'manual-codex'}",
        f"- evidence: {evidence_path or 'not written'}",
        f"- evidence hash: {evidence.get('evidence_hash')}",
    ]
    match_read_path = getattr(result, "codex_match_read_path", None)
    if match_read_path:
        lines.append(f"- match read: {match_read_path}")
    if response.get("match_read_sources"):
        lines.extend(["", "## Match-read sources", ""])
        lines.extend(f"- {src}" for src in response.get("match_read_sources", []))
    if response.get("briefing"):
        lines.extend(["", "## Match briefing", "", str(response.get("briefing"))])
    if response.get("sources"):
        lines.extend(["", "## Match sources", ""])
        lines.extend(f"- {src}" for src in response.get("sources", []))
    _append_subagent_memos(lines, response.get("subagent_memos"))

    context_avail = _context_available(evidence)
    lines.extend(_context_section(evidence, context_avail))

    audits = {p.market_id: p.codex_audit for p in result.predictions}
    evidence_by_market = {
        item["market_id"]: item for item in evidence.get("question_evidence", [])
    }
    lines.extend(["", "## Markets"])
    for market in result.markets:
        mid = market["id"]
        audit = audits.get(mid)
        qe = evidence_by_market.get(mid, {})
        question_id = qe.get("question_id")
        heading = (
            f"{question_id}: {market['question']}"
            if question_id else market["question"]
        )
        lines.extend(["", f"### {heading}"])
        if not audit:
            reason = result.skip_reasons.get(mid, "not priced")
            lines.append(f"- skipped: {reason}")
            continue
        lines.append(f"- base probability: {audit.get('base_probability_int')}%")
        lines.append(f"- final probability: {audit.get('probability_int')}%")
        _append_language_adjustment(lines, audit.get("language_adjustment"))
        _append_audit_list(lines, "Provided odds used", audit.get("provided_odds_used"))
        _append_audit_list(lines, "Online odds found", audit.get("online_odds_found"))
        _append_audit_list(lines, "Non-odds factors", audit.get("non_odds_factors_used"))
        _append_audit_list(lines, "Ignored/downweighted", audit.get("ignored_or_downweighted_evidence"))
        lines.append(f"- reasoning summary: {audit.get('reasoning_summary')}")
        _append_audit_list(lines, "Sources", audit.get("sources"))
        if "player_form" in qe:
            pf = qe["player_form"]
            lines.append("- player form (this player): "
                         + (json.dumps(pf, ensure_ascii=False, sort_keys=True)
                            if pf else "no sample found"))
        if not (audit.get("provided_odds_used") or audit.get("online_odds_found")):
            lines.append("- audit note: no direct or online odds were used; related evidence follows.")
        if qe.get("direct_odds"):
            lines.append(f"- provided direct odds available: {len(qe['direct_odds'])}")
        if qe.get("online_odds_candidates"):
            lines.append(
                f"- pre-collected online odds candidates: "
                f"{len(qe['online_odds_candidates'])}"
            )
        est = qe.get("simulator_estimate")
        if est is None:
            legacy_estimates = qe.get("simulator_model_estimates") or []
            est = legacy_estimates[0] if legacy_estimates else None
        if est:
            baseline = est.get("calibrated_baseline") or {}
            if baseline:
                brier = baseline.get("brier") or {}
                details = []
                if baseline.get("scope"):
                    details.append(f"scope={baseline.get('scope')}")
                if baseline.get("comparison_n") is not None:
                    details.append(f"obs={baseline.get('comparison_n')}")
                if brier:
                    details.append(f"Brier {_brier_summary(brier)}")
                suffix = f" ({'; '.join(details)})" if details else ""
                lines.append(
                    f"- calibrated baseline: {baseline.get('probability_pct')}% "
                    f"from {baseline.get('source')}{suffix}"
                )
            history = est.get("historical_evidence") or {}
            preferred_scope = baseline.get("scope") if baseline else None
            comparison = (
                (est.get("contract_comparison") or {}).get(preferred_scope)
                if preferred_scope else None
            ) or (
                (est.get("contract_comparison") or {}).get("wc2026")
                or (history.get("contract_performance") or {}).get("wc2026")
                or (est.get("contract_comparison") or {}).get("all_history")
                or (history.get("contract_performance") or {}).get("all_history")
                or {}
            )
            if comparison.get("available"):
                brier = comparison.get("brier") or {}
                suffix = (
                    f", contract Brier {_brier_summary(brier)} "
                    f"signal={comparison.get('comparison_signal')} "
                    f"(obs={comparison.get('observations')})"
                )
            elif comparison.get("brier"):
                brier = comparison["brier"]
                suffix = (
                    f", contract Brier {_brier_summary(brier)} "
                    f"signal={comparison.get('signal')} "
                    f"(obs={comparison.get('n_observations')})"
                )
            else:
                legacy = (history.get("model_performance") or {}).get("all_history") or {}
                suffix = (
                    f", all-history Brier {legacy.get('brier')} vs "
                    f"{legacy.get('always_50_brier')} (n={legacy.get('matches')})"
                    if legacy.get("available") else ""
                )
            lines.append(
                f"- simulator estimate: {est.get('probability_pct')}% "
                f"[{est.get('family')} | {est.get('contract_key')}]{suffix}"
            )
        if context_avail:
            lines.append(f"- structured context available: {', '.join(context_avail)}")
    return "\n".join(lines)


def _brier_summary(brier: dict) -> str:
    pieces = []
    for key, label in (
        ("simulator", "sim"),
        ("shrunk_empirical_rate", "shrunk"),
        ("empirical_rate", "emp"),
        ("always_50", "50"),
    ):
        if brier.get(key) is not None:
            pieces.append(f"{label}={brier.get(key)}")
    return " ".join(pieces)


def _context_available(evidence: dict) -> list[str]:
    """Which structured context families are populated for this match."""
    tf = evidence.get("team_form") or {}
    pf = evidence.get("player_form") or {}
    inj = evidence.get("injuries") or {}
    avail = []
    if any((tf.get(s) for s in ("home", "away"))):
        avail.append("team form")
    if any((pf.get(s) for s in ("home", "away"))):
        avail.append("player form")
    if evidence.get("referee_profile"):
        avail.append("referee")
    if any((inj.get(s) for s in ("home", "away"))):
        avail.append("injuries")
    return avail


def _context_section(evidence: dict, context_avail: list[str]) -> list[str]:
    """Human-readable summary of the structured context the model was given."""
    if not context_avail:
        return []
    lines = ["", "## Provided context", "", f"- available: {', '.join(context_avail)}"]
    tf = evidence.get("team_form") or {}
    for side in ("home", "away"):
        form = tf.get(side)
        if form:
            lines.append(f"- {side} form: "
                         + json.dumps(form, ensure_ascii=False, sort_keys=True))
    ref = evidence.get("referee_profile") or {}
    if ref:
        lines.append("- referee: " + json.dumps(ref, ensure_ascii=False, sort_keys=True))
    pf = evidence.get("player_form") or {}
    for side in ("home", "away"):
        players = pf.get(side) or []
        if players:
            names = ", ".join(p.get("name", "?") for p in players[:6])
            lines.append(f"- {side} player form: {len(players)} players ({names}…)")
    inj = evidence.get("injuries") or {}
    for side in ("home", "away"):
        listed = inj.get(side) or []
        if listed:
            lines.append(f"- {side} injuries: {len(listed)} listed")
    return lines


def _append_audit_list(lines: list[str], title: str, items) -> None:
    lines.append(f"- {title}:")
    if not items:
        lines.append("  - none")
        return
    for item in items:
        if isinstance(item, str):
            lines.append(f"  - {item}")
        else:
            lines.append(f"  - {json.dumps(item, ensure_ascii=False, sort_keys=True)}")


def _append_subagent_memos(lines: list[str], memos) -> None:
    if not isinstance(memos, dict):
        return
    lines.extend(["", "## Subagent memos", ""])
    for title, key in (
        ("Base pricing", "base_pricing"),
        ("Match-read aspects", "match_read_aspects"),
        ("Question adjustments", "question_adjustments"),
    ):
        rows = memos.get(key) or []
        lines.append(f"### {title}")
        if not rows:
            lines.append("- none")
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            label = row.get("question_id") or row.get("aspect") or row.get("market_id") or "memo"
            memo = row.get("memo") or row.get("summary") or ""
            lines.append(f"- {label}: {memo}")


def _append_language_adjustment(lines: list[str], adjustment) -> None:
    lines.append("- language adjustment:")
    if not isinstance(adjustment, dict):
        lines.append("  - none")
        return
    summary = {
        key: adjustment.get(key)
        for key in (
            "action", "direction", "move_points", "confidence",
            "base_used", "why_move_or_hold",
        )
    }
    lines.append(f"  - {json.dumps(summary, ensure_ascii=False, sort_keys=True)}")
    evidence_items = adjustment.get("match_read_evidence") or []
    if evidence_items:
        lines.append("  - match_read_evidence:")
        for item in evidence_items:
            lines.append(f"    - {json.dumps(item, ensure_ascii=False, sort_keys=True)}")


def _skip_all(result, reason: str) -> None:
    result.predictions = []
    result.skipped = []
    result.skip_reasons = {}
    for market in result.markets:
        _skip(result, market, reason)


def _skip(result, market: dict, reason: str) -> None:
    result.skipped.append((market["question"], reason))
    result.skip_reasons[market["id"]] = reason


def _slug(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "_", value).strip("_").lower()
    return slug[:80] or "match"
