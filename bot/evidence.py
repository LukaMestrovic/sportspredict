"""Build auditable per-match evidence for the LLM pricing layer.

The evidence file is the deterministic handoff between provider odds and the
final LLM judgement. It contains per-book de-vigged probabilities, raw odds, and
why each observation is relevant to each SportPredict question. Deterministic
derived/empirical estimates may be included as context, but they are not final
anchors and are labeled separately from bookmaker odds.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from . import config, derive
from . import oddsapi as oapi
from . import predictor as afpred
from .matcher import match_intent, match_intent_oddsapi
from .parser import parse_questions
from .pricing import PriceCtx


EVIDENCE_DIR = config.ROOT / "logs" / "llm_pricing_runs"
MAX_RELATED_ODDS_PER_QUESTION = 30


def build_match_evidence(
    result,
    ctx: PriceCtx,
    lineups: list[dict] | None,
    minutes_before: float | None,
) -> dict:
    """Return the full JSON-serialisable evidence bundle for one match."""
    direct_by_market: dict[str, list[dict]] = {}
    spec_by_market: dict[str, dict | None] = {}
    estimates_by_market: dict[str, list[dict]] = {}

    for market in result.markets:
        mid = market["id"]
        intent = result.intents.get(mid)
        direct, spec = _direct_odds(intent, ctx)
        direct_by_market[mid] = _tag_observations(direct, "direct", "exact mapped contract")
        spec_by_market[mid] = spec
        estimates_by_market[mid] = _deterministic_estimates(market["question"], intent, ctx)

    question_evidence = []
    for market in result.markets:
        mid = market["id"]
        question = market["question"]
        intent = result.intents.get(mid)
        direct = direct_by_market[mid]
        related = _related_odds(question, intent, ctx)
        if not direct:
            related.extend(_other_direct_odds(mid, direct_by_market))
        related = _limit_observations(
            [_compact_related_observation(obs) for obs in _dedupe_observations(related, exclude=direct)],
            MAX_RELATED_ODDS_PER_QUESTION,
        )

        question_evidence.append({
            "market_id": mid,
            "question": question,
            "intent": intent,
            "direct_market_spec": spec_by_market[mid],
            "direct_odds": direct,
            "related_odds": related,
            "deterministic_estimates": estimates_by_market[mid],
            "audit_requirement": (
                "The final LLM response must explain which provided odds, online "
                "odds, and non-odds factors were used or downweighted."
            ),
        })

    all_obs = []
    for item in question_evidence:
        all_obs.extend(item["direct_odds"])
        all_obs.extend(item["related_odds"])
    evidence = {
        "schema_version": 1,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "match": _match_meta(result, lineups, minutes_before),
        "questions": [
            {"market_id": m["id"], "question": m["question"],
             "intent": result.intents.get(m["id"])}
            for m in result.markets
        ],
        "question_evidence": question_evidence,
        "provider_odds_summary": _provider_odds_summary(_dedupe_observations(all_obs)),
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


def _related_odds(question: str, intent: dict | None, ctx: PriceCtx) -> list[dict]:
    related = []
    for related_intent, why in _related_intents(question, intent, ctx.home, ctx.away):
        obs, _spec = _direct_odds(related_intent, ctx)
        related.extend(_tag_observations(obs, "related", why))
    return related


def _related_intents(
    question: str, intent: dict | None, home: str, away: str
) -> list[tuple[dict, str]]:
    base = [
        (_intent("match_winner", "home", "win"), f"{home} win price informs team strength"),
        (_intent("match_winner", "away", "win"), f"{away} win price informs team strength"),
        (_intent("match_draw", "match"), "draw price completes the match-result view"),
        (_intent("total_goals", "match", "gte", 3), "goal environment context"),
        (_intent("total_goals", "match", "lte", 2), "low-scoring environment context"),
        (_intent("btts", "match"), "both-teams-to-score context"),
        (_intent("total_corners", "match", "gte", 10), "corner volume context"),
        (_intent("total_cards", "match", "gte", 4), "card temperature context"),
        (_intent("total_shots_on_target", "match", "gte", 8), "shot quality/volume context"),
        (_intent("corners_compare", "home", "more"), f"{home} territorial/corner edge"),
        (_intent("corners_compare", "away", "more"), f"{away} territorial/corner edge"),
        (_intent("cards_compare", "home", "more"), f"{home} card-risk comparison"),
        (_intent("cards_compare", "away", "more"), f"{away} card-risk comparison"),
        (_intent("shots_on_target_compare", "home", "more"), f"{home} shot-on-target edge"),
        (_intent("shots_on_target_compare", "away", "more"), f"{away} shot-on-target edge"),
    ]
    if not intent:
        return base

    market = intent.get("market")
    subject = intent.get("subject")
    period = intent.get("period", "match")
    player = intent.get("player")
    threshold = intent.get("threshold")
    out = list(base)

    if subject in ("home", "away"):
        side_team = home if subject == "home" else away
        out.extend([
            (_intent("team_score", subject), f"{side_team} to-score price informs attacking base"),
            (_intent("team_total_goals", subject, "gte", 1), f"{side_team} goal-total context"),
            (_intent("team_total_goals", subject, "gte", 2), f"{side_team} upside scoring context"),
            (_intent("team_corners", subject, "gte", 5), f"{side_team} corner-volume context"),
            (_intent("team_cards", subject, "gte", 2), f"{side_team} card-volume context"),
            (_intent("team_offsides", subject, "gte", 2), f"{side_team} offside-volume context"),
            (_intent("team_fouls", subject, "gte", 10), f"{side_team} foul-volume context"),
        ])
    if period in ("1H", "2H"):
        out.extend([
            (_intent("match_winner", "home", "win", period=period), f"{period} home edge context"),
            (_intent("match_winner", "away", "win", period=period), f"{period} away edge context"),
            (_intent("match_draw", "match", period=period), f"{period} draw context"),
            (_intent("total_goals", "match", "gte", 1, period=period), f"{period} goal context"),
            (_intent("btts", "match", period=period), f"{period} BTTS context"),
        ])
    if player:
        out.extend([
            (_intent("player_goal_scorer", "player", player=player),
             f"{player} anytime-scorer context"),
            (_intent("player_score_or_assist", "player", player=player),
             f"{player} score-or-assist context"),
            (_intent("player_card", "player", player=player), f"{player} booking context"),
        ])
        if threshold is not None:
            out.append((_intent("player_shots_on_target", "player", "gte", threshold,
                                player=player), f"{player} shots-on-target context"))
    if market == "none":
        out.extend(_compound_component_intents(question, home, away))
    return _dedupe_intents(out)


def _compound_component_intents(question: str, home: str, away: str) -> list[tuple[dict, str]]:
    try:
        split = derive._split(question)  # deterministic templates first, cached LLM fallback.
    except Exception:
        return []
    if not split:
        return []
    out = []
    for key in ("a", "b"):
        subq = split.get(key)
        if not subq:
            continue
        try:
            parsed = parse_questions([{"id": key, "question": subq}], home, away).get(key)
        except Exception:
            parsed = None
        if parsed:
            out.append((parsed, f"component odds for compound leg: {subq}"))
    return out


def _deterministic_estimates(question: str, intent: dict | None, ctx: PriceCtx) -> list[dict]:
    estimates = []
    try:
        if re.search(r"\b(?:AND|OR)\b|\bscore the first goal of the game and\b", question):
            out, source = derive.price_compound(question, ctx)
        else:
            out, source = derive.price_empirical(question, intent, ctx)
    except Exception:
        out = source = None
    if out:
        estimates.append({
            "source": source,
            "label": out.get("label"),
            "probability": round(out["probability"], 6),
            "probability_pct": round(out["probability"] * 100, 2),
            "note": "Deterministic model context only; not a final anchor.",
        })
    return estimates


def _other_direct_odds(market_id: str, direct_by_market: dict[str, list[dict]]) -> list[dict]:
    obs = []
    for other_id, direct in direct_by_market.items():
        if other_id == market_id:
            continue
        obs.extend(_tag_observations(
            direct, "related",
            f"direct odds for another SportPredict market in this match ({other_id})",
        ))
    return obs


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


def _limit_observations(observations: list[dict], limit: int) -> list[dict]:
    """Keep a bounded but diverse related-odds set for prompt size control."""
    if len(observations) <= limit:
        return observations
    buckets: dict[tuple, list[dict]] = {}
    for obs in observations:
        key = (obs.get("source"), obs.get("market_key"), obs.get("contract"))
        buckets.setdefault(key, []).append(obs)
    selected = []
    while len(selected) < limit and buckets:
        for key in list(buckets):
            if buckets[key]:
                selected.append(buckets[key].pop(0))
                if len(selected) >= limit:
                    break
            if not buckets.get(key):
                buckets.pop(key, None)
    return selected


def _compact_related_observation(obs: dict) -> dict:
    """Related odds keep the audit essentials but omit bulky raw outcome arrays."""
    return {
        key: value for key, value in obs.items()
        if key in {
            "source", "bookmaker", "market_key", "market_name", "contract",
            "probability", "probability_pct", "devig_method", "role", "why_relevant",
        }
    }


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


def _dedupe_intents(items: Iterable[tuple[dict, str]]) -> list[tuple[dict, str]]:
    seen = set()
    out = []
    for intent, why in items:
        key = json.dumps(intent, sort_keys=True)
        if key not in seen:
            seen.add(key)
            out.append((intent, why))
    return out


def _intent(
    market: str,
    subject: str = "match",
    comparator: str = "yes",
    threshold: int | None = None,
    period: str = "match",
    player: str | None = None,
) -> dict:
    return {
        "market": market,
        "subject": subject,
        "player": player,
        "comparator": comparator,
        "threshold": threshold,
        "period": period,
    }


def _slug(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "_", value).strip("_").lower()
    return slug[:80] or "match"
