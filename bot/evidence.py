"""Build auditable per-match evidence for the LLM pricing layer.

The evidence file is the deterministic handoff between provider odds and the
raw LLM judgement. It contains per-book de-vigged probabilities and raw odds for
the exact SportPredict contract, or one simulator fallback when no exact price
exists. Broad related-market bundles are deliberately excluded.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from . import config, simulator, wc2026_evidence
from . import oddsapi as oapi
from . import predictor as afpred
from .matcher import match_intent, match_intent_oddsapi
from .pricing import PriceCtx
from .teams import player_matches


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
        direct_by_market[mid] = _tag_observations(direct, "direct", "exact mapped contract")
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

    context = getattr(result, "match_context", None) or {}
    player_index = context.get("player_index") or {}

    question_evidence = []
    for market in result.markets:
        mid = market["id"]
        question = market["question"]
        intent = result.intents.get(mid)
        direct = direct_by_market[mid]
        item = {
            "market_id": mid,
            "question": question,
            "intent": intent,
            "direct_market_spec": spec_by_market[mid],
            "direct_odds": direct,
            "simulator_model_estimates": (
                [simulator_by_market[mid]] if not direct and mid in simulator_by_market else []
            ),
            "audit_requirement": (
                "The raw LLM response must explain which exact provided odds or "
                "fallback simulator context, online odds, and non-odds factors "
                "were used or downweighted."
            ),
        }
        # For a player-specific market, attach THAT player's exact form row so the
        # model cannot read the wrong player's line from the match-level list.
        player_form = _player_form_for(intent, player_index)
        if player_form is not None:
            item["player_form"] = player_form
        question_evidence.append(item)

    all_obs = []
    for item in question_evidence:
        all_obs.extend(item["direct_odds"])
    evidence = {
        "schema_version": 6,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "match": _match_meta(result, lineups, minutes_before),
        "team_form": context.get("team_form") or {},
        "player_form": context.get("player_form") or {},
        "referee_profile": context.get("referee_profile") or {},
        "injuries": context.get("injuries") or {},
        "questions": [
            {"market_id": m["id"], "question": m["question"],
             "intent": result.intents.get(m["id"])}
            for m in result.markets
        ],
        "question_evidence": question_evidence,
        "provider_odds_summary": _provider_odds_summary(_dedupe_observations(all_obs)),
        "wc2026_evidence_refresh": wc2026_refresh,
        "llm_research_requirements": [
            "Find any additional market prices or odds available online, including "
            "Kalshi, Polymarket, Pinnacle, Betfair Exchange, and betting platforms.",
            "Convert every used online price or odd into a probability and cite the URL.",
            "Use only information published before kickoff.",
            "Report tactics, lineups, weather, referee, motivation, and other non-odds "
            "factors that materially affect each probability.",
        ],
    }
    evidence["evidence_hash"] = evidence_hash(evidence)
    return evidence


def write_evidence(evidence: dict, *, directory: Path = EVIDENCE_DIR) -> Path:
    """Persist the evidence JSON and return its path."""
    directory.mkdir(parents=True, exist_ok=True)
    match = evidence.get("match", {})
    kickoff = str(match.get("kickoff") or "unknown").replace(":", "").replace("-", "")
    slug = _slug(f"{match.get('home') or 'home'}_vs_{match.get('away') or 'away'}")
    path = directory / f"{kickoff}_{slug}_{evidence['evidence_hash'][:10]}_evidence.json"
    path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
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


def _player_form_for(intent: dict | None, player_index: dict) -> dict | None:
    """The form row for a player-specific market.

    Returns ``None`` for non-player markets (so no key is added), ``{}`` for a
    named player with no form sample, or the matched player's exact row. Matching
    by name (via ``player_matches``) removes the wrong-row risk of leaving the
    model to pick from the match-level list.
    """
    player = (intent or {}).get("player")
    if not player or player == "None":
        return None
    for row in player_index.values():
        if player_matches(player, row.get("name", "")):
            return row
    return {}


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
    af_spec = match_intent(intent, ctx.home, ctx.away)
    oa_spec = match_intent_oddsapi(intent, ctx.home, ctx.away) if ctx.oa else None
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


def _dedupe_observations(
    observations: Iterable[dict], *, exclude: Iterable[dict] = ()
) -> list[dict]:
    excluded = {_obs_key(obs) for obs in exclude}
    seen = set()
    out = []
    for obs in observations:
        key = _obs_key(obs)
        if key in excluded or key in seen:
            continue
        seen.add(key)
        out.append(obs)
    return out


def _provider_odds_summary(observations: list[dict]) -> dict:
    by_source: dict[str, int] = {}
    by_market: dict[str, int] = {}
    for obs in observations:
        source = obs.get("source") or "unknown"
        market = f"{source}:{obs.get('market_key')}"
        by_source[source] = by_source.get(source, 0) + 1
        by_market[market] = by_market.get(market, 0) + 1
    return {
        "total_observations": len(observations),
        "by_source": by_source,
        "by_market": by_market,
    }


def _obs_key(obs: dict) -> tuple:
    return (
        obs.get("source"), obs.get("bookmaker"), obs.get("market_key"),
        obs.get("contract"), obs.get("probability"),
    )


def _slug(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "_", value).strip("_").lower()
    return slug[:80] or "match"
