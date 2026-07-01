"""Apply referee discipline multipliers to a match context.

The multipliers themselves are built (with shrinkage) in
:func:`sportspredict.ingest.referee.build_referee_multipliers`; this helper looks one up by
name and writes it onto the :class:`MatchContext` so Layer 1 picks it up. Unknown referees
fall back to neutral multipliers (1.0).
"""

from __future__ import annotations

from ..features.context import MatchContext
from ..ingest.referee import referee_key


def apply_referee(ctx: MatchContext, multipliers: dict[str, dict]) -> MatchContext:
    if not ctx.referee:
        return ctx
    # The multipliers dict is keyed by referee_key; accept a raw-name key too so a
    # hand-built table still works.
    m = multipliers.get(referee_key(ctx.referee)) or multipliers.get(ctx.referee)
    if not m:
        return ctx
    ctx.referee_card_mult = float(m.get("card_mult", 1.0))
    ctx.referee_foul_mult = float(m.get("foul_mult", 1.0))
    ctx.referee_pen_mult = float(m.get("pen_mult", 1.0))
    return ctx
