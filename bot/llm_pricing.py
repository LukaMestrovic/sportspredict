"""Auditable LLM pricing from match evidence.

This layer receives the deterministic evidence JSON for one match and returns
the submitted probabilities. The model first prices a base from the evidence,
then writes a researched match read and may move probabilities only through a
complete, validator-checked language-adjustment audit.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

import requests

from . import cache, config
from .pipeline import Prediction


LLM_PRICING_VERSION = "lp7"
MODEL = os.environ.get("LLM_PRICING_MODEL", "gpt-5.5")
REASONING_EFFORT = os.environ.get("LLM_PRICING_REASONING_EFFORT", "high")
SEARCH_CONTEXT_SIZE = os.environ.get("LLM_PRICING_SEARCH_CONTEXT_SIZE", "medium")
PROMPT_PATH = config.ROOT / "prompts" / "llm_pricing_prompt.md"
ENABLED = os.environ.get("LLM_PRICING_ENABLED", "1") != "0"
# Read timeout (seconds) for the single per-match OpenAI call. This model does
# web search + medium reasoning over a large evidence payload, which has been
# observed to exceed 300s; a timeout makes the T-30 cron skip every market for
# that match. 600s is comfortably above the observed need, and even the two
# internal retries (worst case 2x) stay inside the 30-minute (1800s) T-30 window.
TIMEOUT = int(os.environ.get("LLM_PRICING_TIMEOUT", "600"))

_PRICES = {
    "gpt-5.5": (5.0, 30.0), "gpt-5": (1.25, 10.0), "gpt-5-mini": (0.25, 2.0),
    "gpt-5.4-mini": (0.25, 2.0),
    "gpt-4.1": (2.0, 8.0), "gpt-4.1-mini": (0.4, 1.6),
}
_WEB_SEARCH_CALL_USD = 0.01
LAST_USAGE: dict | None = None

REQUIRED_AUDIT_FIELDS = (
    "provided_odds_used",
    "online_odds_found",
    "non_odds_factors_used",
    "ignored_or_downweighted_evidence",
    "reasoning_summary",
    "sources",
)

_prompt_cache: str | None = None


_FALLBACK_PROMPT = """You are a football probability analyst. Price every binary
SportPredict market from the supplied evidence JSON plus web research. Return
only JSON with match_read_markdown, match_read_sources, and a market entry for
every market_id. Include base_probability_int, probability_int 1-99, a complete
language_adjustment audit, provided_odds_used, online_odds_found,
non_odds_factors_used, ignored_or_downweighted_evidence, reasoning_summary, and
sources. When no direct odds exist, use simulator_estimate.calibrated_baseline
as the base when present. Do not mention hidden reasoning or chain-of-thought."""


def _load_prompt() -> str:
    global _prompt_cache
    if _prompt_cache is None:
        try:
            _prompt_cache = PROMPT_PATH.read_text(encoding="utf-8")
        except OSError:
            _prompt_cache = _FALLBACK_PROMPT
    return _prompt_cache


def _cache_key(match_id: str) -> str:
    prompt_sha = _prompt_sha()
    return json.dumps({
        "v": LLM_PRICING_VERSION,
        "model": MODEL,
        "reasoning_effort": REASONING_EFFORT,
        "search_context_size": SEARCH_CONTEXT_SIZE,
        "match_id": match_id,
        "prompt_sha": prompt_sha,
    }, sort_keys=True)


def _prompt_sha() -> str:
    return hashlib.sha1(_load_prompt().encode("utf-8")).hexdigest()[:8]


def _extract_json(text: str) -> dict:
    text = text.strip()
    try:
        return json.loads(text)
    except ValueError:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if not match:
            raise
        return json.loads(match.group(0))


def _record_usage(data: dict) -> None:
    global LAST_USAGE
    usage = data.get("usage") or {}
    in_tok = usage.get("input_tokens") or usage.get("prompt_tokens") or 0
    out_tok = usage.get("output_tokens") or usage.get("completion_tokens") or 0
    web_calls = sum(1 for o in data.get("output", [])
                    if str(o.get("type", "")).startswith("web_search"))
    in_rate, out_rate = _PRICES.get(MODEL, (0.0, 0.0))
    cost = in_tok / 1e6 * in_rate + out_tok / 1e6 * out_rate
    cost += web_calls * _WEB_SEARCH_CALL_USD
    LAST_USAGE = {
        "model": MODEL,
        "input_tokens": in_tok,
        "output_tokens": out_tok,
        "web_search_calls": web_calls,
        "est_cost_usd": round(cost, 4),
    }
    print(f"[llm-pricing] usage model={MODEL} in={in_tok} out={out_tok} "
          f"web_calls={web_calls} est_cost=${cost:.4f}", flush=True)


def _call_llm(evidence: dict) -> dict:
    payload = {
        "model": MODEL,
        "tools": [{"type": "web_search", "search_context_size": SEARCH_CONTEXT_SIZE}],
        "reasoning": {"effort": REASONING_EFFORT},
        "input": f"{_load_prompt()}\n\nMATCH EVIDENCE JSON:\n"
                 f"{json.dumps(evidence, ensure_ascii=False)}",
    }
    last_exc: Exception | None = None
    for _ in range(2):
        try:
            r = requests.post(
                "https://api.openai.com/v1/responses",
                headers={"Authorization": f"Bearer {config.OPENAI_API_KEY}"},
                json=payload,
                timeout=TIMEOUT,
            )
            try:
                r.raise_for_status()
            except requests.HTTPError as exc:
                raise RuntimeError(f"{exc}: {r.text[:500]}") from exc
            data = r.json()
            _record_usage(data)
            text = "".join(
                c.get("text", "")
                for o in data.get("output", []) if o.get("type") == "message"
                for c in o.get("content", [])
            )
            return _extract_json(text)
        except (requests.RequestException, ValueError) as exc:
            last_exc = exc
    raise last_exc  # type: ignore[misc]


def _ask(evidence: dict, *, refresh: bool = False) -> dict:
    key = _cache_key(evidence["match"]["match_id"])
    return cache.get_or_fetch(
        "llm_pricing", key, lambda: _call_llm(evidence), ttl=0, refresh=refresh,
    )


def price_match(
    result,
    evidence: dict,
    evidence_path: Path | None,
    minutes_before: float | None,
    *,
    force: bool = False,
    refresh: bool = False,
):
    """Populate ``result.predictions`` with validated raw LLM probabilities."""
    if not (force or ENABLED) or not config.OPENAI_API_KEY:
        _skip_all(result, "LLM pricing unavailable")
        return result
    if minutes_before is not None and minutes_before <= 0:
        _skip_all(result, "LLM pricing refused after kickoff")
        return result

    try:
        response = _ask(evidence, refresh=refresh) or {}
    except Exception as exc:
        _skip_all(result, f"LLM pricing failed: {exc}")
        return result

    return apply_pricing_response(result, evidence, evidence_path, response)


def apply_pricing_response(
    result,
    evidence: dict,
    evidence_path: Path | None,
    response: dict,
    *,
    require_all_markets: bool = False,
    model_label: str | None = None,
):
    """Validate an audited pricing JSON response and attach predictions."""
    match_read = str(response.get("match_read_markdown") or "").strip()
    if not match_read:
        _skip_all(result, "LLM pricing missing match_read_markdown")
        if require_all_markets:
            raise ValueError("LLM pricing missing match_read_markdown")
        return result

    markets = response.get("markets")
    if not isinstance(markets, list):
        _skip_all(result, "LLM pricing returned no markets list")
        if require_all_markets:
            raise ValueError("LLM pricing returned no markets list")
        return result

    result.llm_pricing_briefing = response.get("briefing")
    result.llm_pricing_sources = response.get("sources") or []
    result.llm_pricing_response = response

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
            _skip(result, market, "LLM pricing omitted market audit")
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
            market_label="LLM audited price",
            source="llm-pricing",
            book_probabilities=[obs["probability"] for obs in direct
                                if isinstance(obs.get("probability"), (int, float))],
        )
        pred.llm_audit = audit
        pred.llm_sources = audit.get("sources") or []
        pred.llm_reasoning_summary = audit.get("reasoning_summary")
        result.predictions.append(pred)

    if require_all_markets and result.skipped:
        problems = "; ".join(f"{q}: {why}" for q, why in result.skipped[:5])
        raise ValueError(f"invalid pricing response: {problems}")

    audit_path, report_path, match_read_path = write_audit_bundle(
        result, evidence, evidence_path, response, model_label=model_label,
    )
    result.llm_pricing_audit_path = str(audit_path)
    result.llm_pricing_report_path = str(report_path)
    result.llm_match_read_path = str(match_read_path)
    return result


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
            return False, f"LLM pricing missing audit field: {field}", 0
        if field == "reasoning_summary" and not str(value).strip():
            return False, "LLM pricing missing reasoning summary", 0
        if field != "reasoning_summary" and not isinstance(value, list):
            return False, f"LLM pricing audit field must be a list: {field}", 0
    ok, reason = _validate_language_adjustment(
        audit, probability_int, base_probability_int, question_evidence or {},
    )
    if not ok:
        return False, reason, 0
    candidates = (question_evidence or {}).get("online_odds_candidates") or []
    if candidates and not _audit_uses_online_candidate(audit, candidates):
        return (
            False,
            "LLM pricing ignored pre-collected online odds candidates",
            0,
        )
    return True, "", probability_int


def _probability_int(value, field: str) -> tuple[bool, str, int]:
    if not isinstance(value, (int, float)):
        return False, f"LLM pricing missing numeric {field}", 0
    rounded = round(float(value))
    if abs(float(value) - rounded) > 1e-9:
        return False, f"LLM pricing {field} must be an integer", 0
    if rounded < 1 or rounded > 99:
        return False, f"LLM pricing {field} outside 1-99", 0
    return True, "", int(rounded)


def _validate_language_adjustment(
    audit: dict,
    probability_int: int,
    base_probability_int: int,
    question_evidence: dict,
) -> tuple[bool, str]:
    adjustment = audit.get("language_adjustment")
    if not isinstance(adjustment, dict):
        return False, "LLM pricing missing language_adjustment object"
    required = (
        "action", "direction", "move_points", "confidence", "base_used",
        "match_read_evidence", "additional_research", "why_move_or_hold",
    )
    for field in required:
        if field not in adjustment:
            return False, f"LLM pricing language_adjustment missing field: {field}"

    ok, reason, base_used = _probability_int(adjustment.get("base_used"), "base_used")
    if not ok:
        return False, reason
    if base_used != base_probability_int:
        return False, "LLM pricing language_adjustment base_used != base_probability_int"

    move_raw = adjustment.get("move_points")
    if not isinstance(move_raw, (int, float)):
        return False, "LLM pricing language_adjustment missing numeric move_points"
    move_points = round(float(move_raw))
    if abs(float(move_raw) - move_points) > 1e-9 or move_points < 0:
        return False, "LLM pricing language_adjustment move_points must be a non-negative integer"

    action = str(adjustment.get("action") or "").lower().strip()
    direction = str(adjustment.get("direction") or "").lower().strip()
    if action not in {"hold", "move"}:
        return False, "LLM pricing language_adjustment action must be hold or move"
    if direction not in {"none", "up", "down"}:
        return False, "LLM pricing language_adjustment direction must be none, up, or down"

    if direction == "up":
        expected = base_probability_int + move_points
    elif direction == "down":
        expected = base_probability_int - move_points
    else:
        expected = base_probability_int
    if expected != probability_int:
        return False, "LLM pricing language_adjustment move does not match final probability"

    if move_points == 0:
        if action != "hold" or direction != "none":
            return False, "LLM pricing zero move must use action=hold and direction=none"
    else:
        if action != "move" or direction == "none":
            return False, "LLM pricing non-zero move must use action=move and direction up/down"
        if not str(adjustment.get("why_move_or_hold") or "").strip():
            return False, "LLM pricing non-zero move missing why_move_or_hold"
        evidence_items = adjustment.get("match_read_evidence")
        if not isinstance(evidence_items, list) or not evidence_items:
            return False, "LLM pricing non-zero move missing match_read_evidence"

    if not str(adjustment.get("why_move_or_hold") or "").strip():
        return False, "LLM pricing language_adjustment missing why_move_or_hold"
    if not isinstance(adjustment.get("match_read_evidence"), list):
        return False, "LLM pricing language_adjustment match_read_evidence must be a list"
    if not isinstance(adjustment.get("additional_research"), list):
        return False, "LLM pricing language_adjustment additional_research must be a list"

    cap = _movement_cap(question_evidence)
    if move_points > cap:
        return False, f"LLM pricing language_adjustment move exceeds {cap} point cap"
    return True, ""


def _movement_cap(question_evidence: dict) -> int:
    primary = ((question_evidence.get("decision_basis") or {}).get("primary") or "")
    if primary == "provided_direct_odds" or question_evidence.get("direct_odds"):
        return 5
    if primary == "pre_collected_online_odds" or question_evidence.get("online_odds_candidates"):
        return 6
    return 10


def _audit_uses_online_candidate(audit: dict, candidates: list[dict]) -> bool:
    online = audit.get("online_odds_found") or []
    if not online:
        return False
    text = json.dumps(online, ensure_ascii=False).lower()
    for candidate in candidates:
        bookmaker = str(candidate.get("bookmaker") or "").lower()
        url = str(candidate.get("url") or "").lower()
        contract = str(candidate.get("contract") or "").lower()
        if bookmaker and bookmaker in text:
            return True
        if url and url in text:
            return True
        if contract and contract in text:
            return True
    return False


def write_audit_bundle(
    result,
    evidence: dict,
    evidence_path: Path | None,
    response: dict,
    *,
    directory: Path | None = None,
    model_label: str | None = None,
) -> tuple[Path, Path, Path]:
    directory = directory or config.ROOT / "logs" / "llm_pricing_runs"
    directory.mkdir(parents=True, exist_ok=True)
    match = evidence.get("match", {})
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    slug = _slug(f"{match.get('home') or 'home'}_vs_{match.get('away') or 'away'}")
    audit_path = directory / f"{stamp}_{slug}_llm_audit.json"
    report_path = directory / f"{stamp}_{slug}_llm_audit.md"
    match_read_path = directory / f"{stamp}_{slug}_match_read.md"

    model = model_label or MODEL
    match_read_text = str(response.get("match_read_markdown") or "").strip()
    match_read_path.write_text(match_read_text + "\n")
    result.llm_match_read_path = str(match_read_path)
    payload = {
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "model": model,
        "evidence_path": str(evidence_path) if evidence_path else None,
        "evidence_hash": evidence.get("evidence_hash"),
        "match_read_path": str(match_read_path),
        "match_read_sources": response.get("match_read_sources") or [],
        "response": response,
        "predictions": [p.llm_audit for p in result.predictions],
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
        f"# LLM pricing audit: {match.get('home')} vs {match.get('away')}",
        "",
        f"- kickoff: {match.get('kickoff')}",
        f"- model: {model_label or MODEL}",
        f"- evidence: {evidence_path or 'not written'}",
        f"- evidence hash: {evidence.get('evidence_hash')}",
    ]
    match_read_path = getattr(result, "llm_match_read_path", None)
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

    context_avail = _context_available(evidence)
    lines.extend(_context_section(evidence, context_avail))

    audits = {p.market_id: p.llm_audit for p in result.predictions}
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
                    details.append(
                        f"Brier sim={brier.get('simulator')} "
                        f"emp={brier.get('empirical_rate')} "
                        f"50={brier.get('always_50')}"
                    )
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
                suffix = (
                    f", contract Brier sim={comparison.get('brier', {}).get('simulator')} "
                    f"emp={comparison.get('brier', {}).get('empirical_rate')} "
                    f"50={comparison.get('brier', {}).get('always_50')} "
                    f"signal={comparison.get('comparison_signal')} "
                    f"(obs={comparison.get('observations')})"
                )
            elif comparison.get("brier"):
                suffix = (
                    f", contract Brier sim={comparison['brier'].get('simulator')} "
                    f"emp={comparison['brier'].get('empirical_rate')} "
                    f"50={comparison['brier'].get('always_50')} "
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
