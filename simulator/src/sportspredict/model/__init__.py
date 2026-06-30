"""Layer 2/3: the generative match simulator and closed forms.

``simulate(rates, n_sims)`` returns a :class:`MatchOutcome` holding vectorized draws for
every modelled quantity; market resolvers reduce it to probabilities.
"""

from .outcome import MatchOutcome
from .simulator import simulate

__all__ = ["MatchOutcome", "simulate"]
