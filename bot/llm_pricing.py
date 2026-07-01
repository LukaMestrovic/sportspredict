"""Auditable LLM pricing from match evidence.

This layer receives the deterministic evidence JSON for one match and returns
the submitted probabilities. It does not tilt anchors: every priced
prediction must have a complete per-market audit showing provided odds used,
online odds found, non-odds factors, downweighted evidence, and a concise
reasoning summary.
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


LLM_PRICING_VERSION = "lp6"
MODEL = os.environ.get("LLM_PRICING_MODEL", "gpt-5.5")
REASONING_EFFORT = os.environ.get("LLM_PRICING_REASONING_EFFORT", "high")
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
only JSON with a market entry for every market_id. Include probability_int 1-99
and a complete audit: provided_odds_used, online_odds_found,
non_odds_factors_used, ignored_or_downweighted_evidence, reasoning_summary, and
sources. Do not mention hidden reasoning or chain-of-thought."""


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
        "tools": [{"type": "web_search", "search_context_size": "low"}],
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
        validated = validate_market_audit(audit)
        if not validated[0]:
            _skip(result, market, validated[1])
            continue
        probability_int = validated[2]
        q_evidence = evidence_by_market.get(mid, {})
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

    audit_path, report_path = write_audit_bundle(
        result, evidence, evidence_path, response, model_label=model_label,
    )
    result.llm_pricing_audit_path = str(audit_path)
    result.llm_pricing_report_path = str(report_path)
    return result


def validate_market_audit(audit: dict) -> tuple[bool, str, int]:
    raw_p = audit.get("probability_int")
    if not isinstance(raw_p, (int, float)):
        return False, "LLM pricing missing numeric probability_int", 0
    probability_int = max(1, min(99, round(float(raw_p))))
    for field in REQUIRED_AUDIT_FIELDS:
        value = audit.get(field)
        if value is None:
            return False, f"LLM pricing missing audit field: {field}", 0
        if field == "reasoning_summary" and not str(value).strip():
            return False, "LLM pricing missing reasoning summary", 0
        if field != "reasoning_summary" and not isinstance(value, list):
            return False, f"LLM pricing audit field must be a list: {field}", 0
    return True, "", probability_int


def write_audit_bundle(
    result,
    evidence: dict,
    evidence_path: Path | None,
    response: dict,
    *,
    directory: Path | None = None,
    model_label: str | None = None,
) -> tuple[Path, Path]:
    directory = directory or config.ROOT / "logs" / "llm_pricing_runs"
    directory.mkdir(parents=True, exist_ok=True)
    match = evidence.get("match", {})
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    slug = _slug(f"{match.get('home') or 'home'}_vs_{match.get('away') or 'away'}")
    audit_path = directory / f"{stamp}_{slug}_llm_audit.json"
    report_path = directory / f"{stamp}_{slug}_llm_audit.md"

    model = model_label or MODEL
    payload = {
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "model": model,
        "evidence_path": str(evidence_path) if evidence_path else None,
        "evidence_hash": evidence.get("evidence_hash"),
        "response": response,
        "predictions": [p.llm_audit for p in result.predictions],
        "skipped": [{"question": q, "why": why} for q, why in result.skipped],
    }
    audit_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    report_path.write_text(
        _markdown_report(result, evidence, evidence_path, response, model_label=model)
        + "\n"
    )
    return audit_path, report_path


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
        lines.extend(["", f"### {market['question']}"])
        if not audit:
            reason = result.skip_reasons.get(mid, "not priced")
            lines.append(f"- skipped: {reason}")
            continue
        lines.append(
            f"- probability: {audit.get('probability_int')}%"
        )
        _append_audit_list(lines, "Provided odds used", audit.get("provided_odds_used"))
        _append_audit_list(lines, "Online odds found", audit.get("online_odds_found"))
        _append_audit_list(lines, "Non-odds factors", audit.get("non_odds_factors_used"))
        _append_audit_list(lines, "Ignored/downweighted", audit.get("ignored_or_downweighted_evidence"))
        lines.append(f"- reasoning summary: {audit.get('reasoning_summary')}")
        _append_audit_list(lines, "Sources", audit.get("sources"))
        qe = evidence_by_market.get(mid, {})
        if "player_form" in qe:
            pf = qe["player_form"]
            lines.append("- player form (this player): "
                         + (json.dumps(pf, ensure_ascii=False, sort_keys=True)
                            if pf else "no sample found"))
        if not (audit.get("provided_odds_used") or audit.get("online_odds_found")):
            lines.append("- audit note: no direct or online odds were used; related evidence follows.")
        if qe.get("direct_odds"):
            lines.append(f"- provided direct odds available: {len(qe['direct_odds'])}")
        est = qe.get("simulator_estimate")
        if est is None:
            legacy_estimates = qe.get("simulator_model_estimates") or []
            est = legacy_estimates[0] if legacy_estimates else None
        if est:
            history = est.get("historical_evidence") or {}
            comparison = (
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
