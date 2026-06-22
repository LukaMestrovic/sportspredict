"""Orchestrates one match end-to-end: questions -> intents -> markets -> probs.

  Parser (LLM)  ->  Market Matcher (catalog)  ->  Predictor (odds de-vig)

Questions with no matching market, or no bookmaker coverage, are skipped
(predict nothing), per the competition spec.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from . import derive, external
from .apifootball import APIFootball
from .oddsapi import OddsAPI
from .parser import parse_questions
from .pricing import PriceCtx, price_intent
from .sportspredict import SportPredict


_COMPOUND_RE = re.compile(r"\b(?:AND|OR)\b|\bscore the first goal of the game and\b")


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


def _clamp_int(p: float) -> int:
    return max(1, min(99, round(p * 100)))


def run_match(
    sp_match: dict,
    markets: list[dict],
    af: APIFootball,
    oa: OddsAPI | None = None,
    *,
    allow_external: bool = True,
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
    )

    kickoff = sp_match["opening_time"]
    for m in markets:
        q = m["question"]
        intent = intents.get(m["id"])
        out = src = spec = None
        skip_reason = "no source could price it"
        if _COMPOUND_RE.search(q):
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
        # 3) last resort: web-grounded external estimate
        if not out and allow_external:
            out, src = external.estimate(q, home, away, kickoff)
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
) -> list[dict]:
    """Submit all priced results in API-sized batches and return the payload."""
    batch = [
        {"market_id": p.market_id, "lobby_id": lobby_id,
         "probability": p.probability_int}
        for result in results for p in result.predictions
    ]
    for start in range(0, len(batch), 50):
        sp.submit_batch(batch[start:start + 50])
    return batch


def predict_open_matches(submit: bool = False, limit: int | None = None):
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
        results.append(run_match(sp_match, markets, af, oa))

    if submit:
        submit_predictions(sp, lobby["id"], results)
    return results
