"""Market resolution layer: NL parsing, the canonical spec, resolvers, de-vig, shrink."""

from .devig import devig
from .parser import ParseError, parse_question
from .resolvers import resolve
from .schema import MarketSpec, MarketType
from .shrink import shrink_to_market

__all__ = [
    "MarketSpec",
    "MarketType",
    "ParseError",
    "parse_question",
    "resolve",
    "devig",
    "shrink_to_market",
]
