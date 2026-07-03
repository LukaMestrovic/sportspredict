"""Orchestrates one match end-to-end.

Live path:
  questions -> parser -> evidence -> audited LLM prices

The older deterministic cascade is retained only for explicit validation/backtest
calls that must not run web-grounded LLM pricing after kickoff.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime, timezone

from . import derive, evidence, ledger, lineups as lineup_fetcher, match_context
from .apifootball import APIFootball
from .oddsapi import OddsAPI
from .parser import parse_questions
from .pricing import PriceCtx, price_intent
from .sportspredict import SportPredict

@dataclass
class Prediction:
    market_id: str
    question: str
    probability: float          # 0-1
    probability_int: int        # 1-99 (what we submit)
    n_books: int
    market_label: str
    source: str = "api-football"  # odds or derivation source that priced it
    book_probabilities: list[float] = field(default_factory=list)
    # Final LLM pricing audit fields.
    llm_audit: dict = field(default_factory=dict)
    llm_sources: list = field(default_factory=list)
    llm_reasoning_summary: str | None = None


@dataclass
class MatchResult:
    sp_match: dict
    fixture: dict | None
    home: str | None
    away: str | None
    predictions: list[Prediction] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)  # (question, why)
    markets: list[dict] = field(default_factory=list)
    intents: dict[str, dict] = field(default_factory=dict)
    market_specs: dict[str, dict | None] = field(default_factory=dict)
    skip_reasons: dict[str, str] = field(default_factory=dict)
    af_books: list[dict] = field(default_factory=list)
    oa_observations: list[dict] = field(default_factory=list)
    evidence_json: dict | None = None
    evidence_path: str | None = None
    evidence_hash: str | None = None
    match_context: dict | None = None
    llm_pricing_briefing: str | None = None
    llm_pricing_sources: list = field(default_factory=list)
    llm_pricing_response: dict | None = None
    llm_pricing_audit_path: str | None = None
    llm_pricing_report_path: str | None = None
    llm_match_read_path: str | None = None


class PlatformVerificationError(RuntimeError):
    def __init__(self, verification: dict):
        super().__init__(f"SportPredict platform verification failed: {verification}")
        self.verification = verification


def _clamp_int(p: float) -> int:
    return max(1, min(99, round(p * 100)))


def run_match(
    sp_match: dict,
    markets: list[dict],
    af: APIFootball,
    oa: OddsAPI | None = None,
    *,
    llm_pricing_enabled: bool = True,
    llm_pricing_refresh: bool = False,
    llm_pricing_call: bool = True,
    lineups: list[dict] | None = None,
    minutes_before: float | None = None,
) -> MatchResult:
    fixture = af.find_fixture(sp_match["opening_time"], sp_match.get("name"))
    res = MatchResult(
        sp_match=sp_match, fixture=fixture, home=None, away=None, markets=markets,
    )

    if not fixture:
        res.skipped = [(m["question"], "no API-Football fixture") for m in markets]
        res.skip_reasons = {
            m["id"]: "no API-Football fixture" for m in markets
        }
        return res

    home = fixture["teams"]["home"]["name"]
    away = fixture["teams"]["away"]["name"]
    res.home, res.away = home, away

    intents = parse_questions(markets, home, away)
    res.intents = intents
    ctx = PriceCtx(
        home=home, away=away,
        af_books=af.odds(fixture["fixture"]["id"]),
        oa=oa,
        oa_event=oa.find_event(sp_match["opening_time"], home, away) if oa else None,
        stage=_fixture_stage(fixture),
    )
    res.af_books = ctx.af_books

    if llm_pricing_enabled:
        from . import llm_pricing

        if minutes_before is None:
            kickoff_dt = datetime.fromisoformat(
                sp_match["opening_time"].replace("Z", "+00:00")
            )
            minutes_before = (
                kickoff_dt - datetime.now(timezone.utc)
            ).total_seconds() / 60.0
        if not lineups:
            lineups = lineup_fetcher.fetch_lineups(
                af, fixture, refresh=llm_pricing_refresh,
            )
        try:
            res.match_context = match_context.build(af, fixture, home, away, lineups)
        except Exception:
            res.match_context = {}
        bundle = evidence.build_match_evidence(res, ctx, lineups, minutes_before, af=af)
        path = evidence.write_evidence(bundle)
        res.evidence_json = bundle
        res.evidence_path = str(path)
        res.evidence_hash = bundle.get("evidence_hash")
        res.market_specs = {
            item["market_id"]: item.get("direct_market_spec")
            for item in bundle.get("question_evidence", [])
        }
        res.oa_observations = list(getattr(ctx.oa, "observations", [])) if ctx.oa else []
        if llm_pricing_call:
            llm_pricing.price_match(
                res, bundle, path, minutes_before, refresh=llm_pricing_refresh,
            )
        return res

    for m in markets:
        q = m["question"]
        intent = intents.get(m["id"])
        out = src = spec = None
        skip_reason = "no source could price it"
        if derive.is_compound_question(q):
            # 1) compound -> derive from the two component markets
            out, src = derive.price_compound(q, ctx)
            if not out:
                out, src = derive.price_empirical(q, intent, ctx)
            skip_reason = "compound component unavailable"
        else:
            # 2) single market: API-Football -> Odds API
            if intent:
                out, src, spec = price_intent(intent, ctx)
                if intent.get("market") == "none":
                    skip_reason = "parser marked unsupported"
                elif spec:
                    skip_reason = "mapped contract or line unavailable"
                else:
                    skip_reason = "no direct market mapping"
            else:
                skip_reason = "parser returned no intent"
            if not out:
                out, src = derive.price_empirical(q, intent, ctx)
        res.market_specs[m["id"]] = spec
        if out:
            res.predictions.append(_mk_pred(m, out, src))
        else:
            res.skipped.append((q, skip_reason))
            res.skip_reasons[m["id"]] = skip_reason
    res.af_books = ctx.af_books
    res.oa_observations = list(getattr(ctx.oa, "observations", []))
    return res


def _mk_pred(m: dict, out: dict, source: str) -> Prediction:
    return Prediction(
        market_id=m["id"], question=m["question"],
        probability=out["probability"], probability_int=_clamp_int(out["probability"]),
        n_books=out["n_books"], market_label=out["label"], source=source,
        book_probabilities=out.get("book_probabilities", []),
    )


def submit_predictions(
    sp: SportPredict, lobby_id: str, results: list[MatchResult]
) -> dict:
    """Upsert all priced predictions and return a result summary.

    SportPredict allows only one prediction per market, so a market that already
    has a prediction (the whole lobby is pre-seeded with a baseline) must be
    PATCHed, not re-POSTed — a plain create is silently rejected per-item. We
    therefore look up our existing predictions, POST genuinely-new markets, and
    PATCH the ones whose probability moved.

    Returns ``{payload, submitted, updated, unchanged, failed, errors}``.
    """
    payload = [
        {"market_id": p.market_id, "lobby_id": lobby_id,
         "probability": p.probability_int}
        for result in results for p in result.predictions
    ]
    existing: dict[str, tuple[str, int]] = {}
    for p in sp.list_predictions(lobby_id):
        mid = p.get("market_id")
        if mid and p.get("market_status", "open") == "open":
            existing[mid] = (p["id"], int(round(float(p.get("probability") or 0))))

    summary = {"payload": payload, "submitted": 0, "updated": 0,
               "unchanged": 0, "failed": 0, "errors": []}

    # POST markets we have no prediction on yet (in API-sized batches).
    new = [e for e in payload if e["market_id"] not in existing]
    for start in range(0, len(new), 50):
        chunk = new[start:start + 50]
        try:
            res = sp.submit_batch(chunk)
        except Exception as exc:  # network/4xx on the whole batch
            summary["failed"] += len(chunk)
            summary["errors"].append({"batch": len(chunk), "error": str(exc)})
            continue
        summary["submitted"] += res.get("succeeded", 0)
        for r in res.get("results", []):
            if not r.get("success"):
                summary["failed"] += 1
                summary["errors"].append(r)

    # PATCH markets that already have a prediction whose probability changed.
    for e in payload:
        prior = existing.get(e["market_id"])
        if prior is None:
            continue
        pred_id, old_prob = prior
        if old_prob == e["probability"]:
            summary["unchanged"] += 1
            continue
        try:
            sp.update_prediction(pred_id, e["probability"])
            summary["updated"] += 1
        except Exception as exc:
            summary["failed"] += 1
            summary["errors"].append({"market_id": e["market_id"], "error": str(exc)})
    return summary


def submit_with_ledger(
    sp: SportPredict,
    event_id: str,
    lobby_id: str,
    results: list[MatchResult],
    *,
    window_min: int = -1,
    minutes_before: float | None = None,
) -> tuple[dict, list[str]]:
    """Record priced runs, upsert them, and durably record the outcome."""
    now = datetime.now(timezone.utc)
    run_ids = []
    for result in results:
        match_minutes = minutes_before
        if match_minutes is None:
            kickoff = datetime.fromisoformat(
                result.sp_match["opening_time"].replace("Z", "+00:00")
            )
            match_minutes = (kickoff - now).total_seconds() / 60.0
        run_ids.append(ledger.record_run(
            event_id, lobby_id, result, window_min, match_minutes,
        ))
    try:
        summary = submit_predictions(sp, lobby_id, results)
    except Exception as exc:
        for run_id in run_ids:
            ledger.mark_failed(run_id, str(exc))
        raise
    # "landed" = our intended value is now on the platform (newly created,
    # patched, or already equal). Only flag the run failed if nothing landed.
    landed = summary["submitted"] + summary["updated"] + summary["unchanged"]
    if not summary["payload"] or landed == 0:
        message = (
            "no predictions to submit" if not summary["payload"]
            else f"0 landed, {summary['failed']} rejected: {summary['errors'][:1]}"
        )
        for run_id in run_ids:
            ledger.mark_failed(run_id, message)
        return summary, run_ids

    verification = verify_platform_predictions(sp, lobby_id, summary["payload"])
    summary["platform_verification"] = verification
    if not verification["ok"]:
        for run_id in run_ids:
            ledger.mark_failed(run_id, json_dumps_compact(verification))
        raise PlatformVerificationError(verification)

    for run_id in run_ids:
        ledger.mark_submitted(run_id)
    return summary, run_ids


def verify_platform_predictions(
    sp: SportPredict,
    lobby_id: str,
    payload: list[dict],
) -> dict:
    """Confirm SportPredict has every intended open-market probability."""
    expected = {
        item["market_id"]: int(item["probability"])
        for item in payload
        if item.get("market_id") is not None
    }
    rows = sp.list_predictions(lobby_id)
    open_by_market = {}
    ignored_closed = []
    for row in rows:
        mid = row.get("market_id")
        if not mid:
            continue
        status = row.get("market_status", "open")
        if status != "open":
            if mid in expected:
                ignored_closed.append({"market_id": mid, "market_status": status})
            continue
        open_by_market[mid] = row

    missing = []
    mismatched = []
    for mid, prob in expected.items():
        row = open_by_market.get(mid)
        if row is None:
            missing.append(mid)
            continue
        actual = _platform_probability_int(row)
        if actual != prob:
            mismatched.append({
                "market_id": mid,
                "expected": prob,
                "actual": actual,
            })
    return {
        "ok": not missing and not mismatched,
        "checked": len(expected) - len(missing),
        "expected": len(expected),
        "missing": missing,
        "mismatched": mismatched,
        "ignored_closed": ignored_closed,
    }


def _platform_probability_int(row: dict) -> int | None:
    value = row.get("probability")
    if value is None:
        value = row.get("probability_submitted")
    if value is None:
        return None
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        return None


def json_dumps_compact(value) -> str:
    import json

    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def predict_open_matches(
    submit: bool = False,
    limit: int | None = None,
    llm_pricing_enabled: bool = True,
):
    """Run the pipeline over all open SP matches. Optionally submit predictions."""
    sp = SportPredict()
    af = APIFootball()
    oa = OddsAPI()
    event = sp.event()
    lobby = sp.lobby(event["id"])
    matches = sp.matches(event["id"], lobby["id"])
    if limit:
        matches = matches[:limit]

    results = []
    for sp_match in matches:
        markets = sp.markets(lobby["id"], sp_match["id"])
        result = run_match(
            sp_match, markets, af, oa,
            llm_pricing_enabled=llm_pricing_enabled,
        )
        results.append(result)

    if submit:
        submit_with_ledger(sp, event["id"], lobby["id"], results)
    return results


def _fixture_stage(fixture: dict) -> str | None:
    round_name = str((fixture.get("league") or {}).get("round") or "").lower()
    if "group" in round_name:
        return "group"
    if round_name:
        return "knockout"
    return None
