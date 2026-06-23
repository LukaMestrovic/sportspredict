"""Shared pricing primitives: price one parsed intent against the odds sources.

For markets both providers quote, we de-vig each provider's books and average
across **all** of them for the deepest consensus — API-Football is free and the
sole source for many lines (offsides, fouls, half periods, compares), while the
Odds API carries far deeper books on the core lines (h2h, totals). Player YES/NO
props stay Odds-API-first because it is lineup-aware. Used by the main pipeline
and by the derivation layer (which prices the components of a compound question).
"""
from __future__ import annotations

from dataclasses import dataclass
from statistics import mean

from . import oddsapi as oapi
from . import predictor as afpred
from .matcher import match_intent, match_intent_oddsapi
from .oddsapi import OddsAPI


@dataclass
class PriceCtx:
    home: str
    away: str
    af_books: list
    oa: OddsAPI | None
    oa_event: dict | None


def _book_probs(out: dict | None) -> list[float]:
    if not out:
        return []
    bp = out.get("book_probabilities")
    return list(bp) if bp else [out["probability"]] * out.get("n_books", 1)


def _merge(af_out: dict | None, oa_out: dict | None) -> dict | None:
    """Average the de-vigged per-book probabilities from both providers.

    Each provider already de-vigs per book and contract, so the fair
    probabilities are comparable; pooling them just widens the book sample.
    """
    probs = _book_probs(af_out) + _book_probs(oa_out)
    if not probs:
        return None
    label = (af_out or oa_out).get("label", "")
    return {"probability": mean(probs), "n_books": len(probs),
            "book_probabilities": probs, "label": label}


def price_intent(intent: dict, ctx: PriceCtx):
    """Price one intent from both sources combined. Returns (out, source, spec)."""
    af_spec = match_intent(intent, ctx.home, ctx.away)
    oa_spec = match_intent_oddsapi(intent, ctx.home, ctx.away) if ctx.oa else None

    def oa_books(spec):
        return ctx.oa.event_odds(ctx.oa_event["id"], [spec["market"]])

    # Player YES/NO props (anytime scorer, score-or-assist, card): the Odds API
    # is the multi-book, lineup-aware specialist. API-Football rarely quotes them
    # and, when it does, often from a single stale PRE-MATCH book that still
    # prices a player who isn't in the XI (a scratched striker left at ~1.80 ->
    # 51%). Price these from the Odds API alone; if that market is quoted there
    # but omits the player, he isn't in the lineup -> skip rather than fall back
    # to the stale lone AF book. Only when the market is not offered at all do we
    # fall through to API-Football.
    if oa_spec and oa_spec.get("kind") == "player_yesno" and ctx.oa_event:
        books = oa_books(oa_spec)
        out = oapi.predict(books, oa_spec)
        if out:
            return out, "odds-api", oa_spec
        if oapi.market_present(books, oa_spec["market"]):
            return None, None, oa_spec

    # Every other market: pool API-Football and Odds API books for one consensus.
    af_out = afpred.predict(ctx.af_books, af_spec) if (af_spec and ctx.af_books) else None
    oa_out = oapi.predict(oa_books(oa_spec), oa_spec) if (oa_spec and ctx.oa_event) else None
    out = _merge(af_out, oa_out)
    if out:
        source = ("af+oa" if af_out and oa_out
                  else "api-football" if af_out else "odds-api")
        return out, source, (af_spec if af_out else oa_spec)
    return None, None, (af_spec or oa_spec)
