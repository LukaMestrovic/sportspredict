"""Shared pricing primitives: price one parsed intent against the odds sources.

Cascade: API-Football → Odds API. Used by the main pipeline and by the
derivation layer (which prices the components of a compound question).
"""
from __future__ import annotations

from dataclasses import dataclass

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


def price_intent(intent: dict, ctx: PriceCtx):
    """Price one intent: API-Football, then Odds API. Returns (out, source, spec)."""
    af_spec = match_intent(intent, ctx.home, ctx.away)
    out = afpred.predict(ctx.af_books, af_spec) if (af_spec and ctx.af_books) else None
    if out:
        return out, "api-football", af_spec
    oa_spec = match_intent_oddsapi(intent, ctx.home, ctx.away) if ctx.oa else None
    if oa_spec and ctx.oa_event:
        books = ctx.oa.event_odds(ctx.oa_event["id"], [oa_spec["market"]])
        out = oapi.predict(books, oa_spec)
        if out:
            return out, "odds-api", oa_spec
    return None, None, (af_spec or oa_spec)
