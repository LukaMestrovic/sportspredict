"""Layer 1: per-team, per-half expected rates as a function of strength, venue, referee."""

from .baseline import RateModel
from .params import MatchRates

__all__ = ["RateModel", "MatchRates"]
