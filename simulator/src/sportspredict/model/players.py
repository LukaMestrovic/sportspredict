"""Player score-or-assist attribution.

Given a team's simulated per-match goal totals, we attribute goals to scorers (multinomial
over per-90 goal-weight shares) and assists to assisters (a fraction ``rho`` of goals are
assisted). For the *score-or-assist* market we need ``P(player scores >=1 OR assists >=1)``,
which we compute **analytically conditional on each sim's team goal count** and average over
sims — a Rao-Blackwellized estimator with far lower variance than drawing per-goal owners:

    P(score or assist | k goals) = 1 - ((1 - p_goal) * (1 - q_assist))^k

where ``p_goal`` is the player's share of team goals and ``q_assist = rho * a_share`` is the
chance a given goal is assisted by the player. Conditioning on ``k`` (which is correlated with
the match result via the shared frailties) preserves the player↔team-success correlation.
"""

from __future__ import annotations

import numpy as np

from ..config import Settings, default_settings
from ..features.context import PlayerInfo
from ..types import H1, H2
from .outcome import MatchOutcome


def _weights(p: PlayerInfo, cfg: dict) -> tuple[float, float]:
    """Return (goal_weight, assist_weight) for a player, scaled by expected exposure."""
    gr = p.goal_rate if p.goal_rate is not None else cfg["default_goal_rate"].get(p.position, 0.0)
    ar = p.assist_rate if p.assist_rate is not None else cfg["default_assist_rate"].get(p.position, 0.0)
    if p.penalty_taker:
        gr = gr * float(cfg.get("penalty_taker_goal_mult", 1.0))
    exposure = p.start_prob
    if p.expected_minutes is not None:
        exposure = max(p.start_prob, p.expected_minutes / 90.0)
    return gr * exposure, ar * exposure


def _ensure_full_team(
    lineup: list[PlayerInfo] | None, target: PlayerInfo, cfg: dict
) -> list[PlayerInfo]:
    """Return the team padded up to a canonical 11.

    A partial lineup (e.g. only the named star) would otherwise hand that player an
    inflated goal share, so we add generic teammates of the missing positions until each
    position reaches its canonical count. A complete lineup is returned unchanged.
    """
    formation = cfg["canonical_formation"]
    players = list(lineup) if lineup else []
    if not any(p is target or p.name == target.name for p in players):
        players = [target, *players]

    have: dict[str, int] = {}
    for p in players:
        have[p.position] = have.get(p.position, 0) + 1
    for pos, n in formation.items():
        while have.get(pos, 0) < int(n):
            players.append(PlayerInfo(name=f"_{pos}{have.get(pos, 0)}", team=target.team, position=pos))
            have[pos] = have.get(pos, 0) + 1
    return players


def _team_goal_counts(
    outcome: MatchOutcome, team_index: int, include_et: bool, half: str
) -> np.ndarray:
    if half in ("1H", "2H"):
        return outcome.goals_half(team_index, H1 if half == "1H" else H2).astype(float)
    return outcome.goals_team(team_index, include_et=include_et).astype(float)


def _shares(
    outcome: MatchOutcome,
    team_index: int,
    target: PlayerInfo,
    lineup: list[PlayerInfo] | None,
    include_et: bool,
    settings: Settings,
    half: str = "full",
) -> tuple[float, float, np.ndarray]:
    """Return (goal share p_goal, per-goal assist chance q_assist, team goal counts k)."""
    cfg = settings.players
    rho = float(cfg["prob_goal_assisted"])
    team = _ensure_full_team(lineup, target, cfg)
    gw = np.array([_weights(p, cfg)[0] for p in team], dtype=float)
    aw = np.array([_weights(p, cfg)[1] for p in team], dtype=float)
    try:
        ti = next(i for i, p in enumerate(team) if p is target or p.name == target.name)
    except StopIteration:
        ti = 0
    p_goal = gw[ti] / gw.sum() if gw.sum() > 0 else 0.0
    a_share = aw[ti] / aw.sum() if aw.sum() > 0 else 0.0
    k = _team_goal_counts(outcome, team_index, include_et, half)
    return p_goal, rho * a_share, k


def prob_score_or_assist(
    outcome: MatchOutcome,
    team_index: int,
    target: PlayerInfo,
    lineup: list[PlayerInfo] | None = None,
    include_et: bool = True,
    half: str = "full",
    settings: Settings | None = None,
) -> float:
    """Probability the target player scores or assists, over the simulated matches."""
    settings = settings or default_settings()
    p_goal, q_assist, k = _shares(outcome, team_index, target, lineup, include_et, settings, half)
    return float(np.mean(1.0 - ((1.0 - p_goal) * (1.0 - q_assist)) ** k))


def prob_score(
    outcome: MatchOutcome,
    team_index: int,
    target: PlayerInfo,
    lineup: list[PlayerInfo] | None = None,
    include_et: bool = True,
    half: str = "full",
    settings: Settings | None = None,
) -> float:
    """Probability the target player scores a goal (own goals excluded)."""
    settings = settings or default_settings()
    p_goal, _q, k = _shares(outcome, team_index, target, lineup, include_et, settings, half)
    return float(np.mean(1.0 - (1.0 - p_goal) ** k))
