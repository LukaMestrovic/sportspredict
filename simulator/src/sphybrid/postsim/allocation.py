"""Post-simulation player count-prop allocation.

The wheel prices a player count prop (e.g. "<player> 2+ shots on target") with a standalone
position-Poisson that is *independent* of the match simulation, so the player market and the team
market for the same statistic can drift apart. The fix scoped in ``docs/player_props_feasibility.md``
is an allocation layer: the player's count is a **share of the same simulated team total** (which the
odds anchor has already corrected), keeping player and team markets coherent.

Given the team's simulated total ``T`` for a statistic (an array over worlds) and a per-unit
ownership probability ``p = share * exposure``, the player's count is ``Binomial(T, p)`` conditional
on ``T`` — the natural generalisation of the wheel's score-or-assist estimator (which uses the
``k`` goals, i.e. the ``>=1`` case ``1-(1-p)^T``). Then::

    P(player meets the line) = mean_w  P(Binomial(T_w, p) <comparator> threshold).

``share`` is read from the validated ``player_shares.json`` (keyed player+stat); unseen players
fall back to a position prior renormalised over the canonical XI. The layer is enabled after
leave-one-tournament-out validation showed better Brier for both 1+ and 2+ shots on target.
"""

from __future__ import annotations

import unicodedata
import json
from pathlib import Path

import numpy as np

from sportspredict.config import Settings, default_settings
from sportspredict.model.outcome import MatchOutcome
from sportspredict.types import H1, H2, TEAM_A, TEAM_B

_LABEL = {"A": TEAM_A, "B": TEAM_B}


def _fold(s: str) -> str:
    s = (s or "").translate(str.maketrans({"Ø": "O", "ø": "o", "Ł": "L", "ł": "l",
                                            "Đ": "D", "đ": "d", "Þ": "Th", "þ": "th"}))
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower().strip()


class PlayerShares:
    """Fitted per-(player, stat) team-total shares loaded from compact JSON."""

    def __init__(
        self, table: dict[tuple[str, str], float], teams: dict[str, str] | None = None,
    ):
        self._m = table
        self._teams = teams or {}

    def get(self, player: str, stat: str) -> float | None:
        return self._m.get((_fold(player), str(stat)))

    def team(self, player: str) -> str | None:
        return self._teams.get(_fold(player))

    @classmethod
    def load(cls, path: str | Path | None) -> "PlayerShares | None":
        if not path or not Path(path).exists():
            return None
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, TypeError):
            return None
        if data.get("columns") != ["player", "team", "stat", "share"]:
            return None
        rows = data.get("rows") or []
        table = {(_fold(str(player)), str(stat)): float(share)
                 for player, _team, stat, share in rows}
        teams = {_fold(str(player)): str(team)
                 for player, team, _stat, _share in rows if team}
        return cls(table, teams) if table else None


def _position_prior_lambdas(stat: str, settings: Settings) -> dict[str, float]:
    if stat == "goals":
        return {k: float(v) for k, v in settings.players.get("default_goal_rate", {}).items()}
    if stat == "assists":
        return {k: float(v) for k, v in settings.players.get("default_assist_rate", {}).items()}
    return {
        k: float(v)
        for k, v in settings.raw.get("player_stat_lambda", {}).get(stat, {}).items()
    }


def position_prior_share(position: str, stat: str, settings: Settings) -> float | None:
    """Position prior as a *share of the team total*: lambda_pos / sum_over_XI(lambda)."""
    cfg = _position_prior_lambdas(stat, settings)
    if not cfg:
        return None
    formation = settings.players.get("canonical_formation", {"GK": 1, "DF": 4, "MF": 4, "FW": 2})
    fallback = float(cfg.get("FW", 0.0))
    team_total = sum(int(n) * float(cfg.get(pos, fallback)) for pos, n in formation.items())
    if team_total <= 0:
        return None
    return float(cfg.get(position, fallback)) / team_total


def _team_share_vector(ctx, team_idx: int, stat: str, shares: PlayerShares | None,
                       settings: Settings) -> tuple[np.ndarray, np.ndarray]:
    """Normalised event-owner shares and a parallel bench flag for one side."""
    players = list(ctx.lineup_for(team_idx))
    values: list[float] = []
    bench: list[bool] = []
    if players:
        for player in players:
            share = shares.get(player.name, stat) if shares is not None else None
            if share is None:
                share = position_prior_share(player.position, stat, settings) or 0.0
            exposure = max(float(player.start_prob), float(player.expected_minutes or 0.0) / 90.0)
            values.append(max(float(share) * exposure, 0.0))
            bench.append(float(player.start_prob) < 0.5)
    else:
        formation = settings.players.get(
            "canonical_formation", {"GK": 1, "DF": 4, "MF": 4, "FW": 2}
        )
        for position, n in formation.items():
            values.extend([position_prior_share(position, stat, settings) or 0.0] * int(n))
            bench.extend([False] * int(n))
    arr = np.asarray(values, dtype=float)
    if arr.size == 0 or arr.sum() <= 0:
        arr = np.full(11, 1.0 / 11.0)
        bench = [False] * 11
    else:
        arr /= arr.sum()
    return arr, np.asarray(bench, dtype=bool)


def _prob_max_at_least(total: int, probs: np.ndarray, threshold: int) -> float:
    """Exact multinomial occupancy probability P(max player count >= threshold)."""
    import math

    if threshold <= 0:
        return 1.0
    if total < threshold:
        return 0.0
    # Coefficient of x^total in product_i sum_{k<threshold} p_i^k/k!, times total!.
    coeff = np.asarray([1.0])
    for p in probs:
        poly = np.asarray([float(p) ** k / math.factorial(k) for k in range(threshold)])
        coeff = np.convolve(coeff, poly)[: total + 1]
    below = math.factorial(total) * coeff[total] if total < len(coeff) else 0.0
    return float(np.clip(1.0 - below, 0.0, 1.0))


def prob_any_player_threshold(
    outcome: MatchOutcome, ctx, stat: str, comparator: str, threshold: float,
    shares: PlayerShares | None, settings: Settings, *, unassigned_share: float = 0.0,
) -> float:
    """Probability any player on either team meets a regulation count threshold."""
    target = int(np.ceil(threshold)) if comparator == ">=" else int(np.floor(threshold)) + 1
    if comparator not in (">=", ">"):
        raise ValueError("any-player markets currently support lower-tail complements only")
    per_team = []
    for team_idx in (TEAM_A, TEAM_B):
        probs, _ = _team_share_vector(ctx, team_idx, stat, shares, settings)
        totals = np.asarray(outcome.team_total(stat, team_idx, include_et=False), dtype=int)
        if unassigned_share > 0:
            from scipy import stats

            retained = float(np.clip(1.0 - unassigned_share, 0.0, 1.0))
            lookup = {}
            for total in np.unique(totals):
                n = int(total)
                lookup[n] = sum(
                    float(stats.binom.pmf(owned, n, retained))
                    * _prob_max_at_least(owned, probs, target)
                    for owned in range(n + 1)
                )
        else:
            lookup = {int(t): _prob_max_at_least(int(t), probs, target) for t in np.unique(totals)}
        per_team.append(np.asarray([lookup[int(t)] for t in totals]))
    return float(np.mean(1.0 - (1.0 - per_team[0]) * (1.0 - per_team[1])))


def prob_substitute_scores(
    outcome: MatchOutcome, ctx, shares: PlayerShares | None, settings: Settings,
    *, fallback_share: float,
) -> float:
    """Probability a bench player scores, conditional on the simulated regulation goal totals."""
    no_sub_goal = np.ones(outcome.n_sims, dtype=float)
    for team_idx in (TEAM_A, TEAM_B):
        probs, bench = _team_share_vector(ctx, team_idx, "goals", shares, settings)
        sub_share = float(probs[bench].sum()) if bench.any() else float(fallback_share)
        sub_share = float(np.clip(sub_share, 0.01, 0.65))
        goals = np.asarray(outcome.goals_team(team_idx, include_et=False), dtype=int)
        no_sub_goal *= (1.0 - sub_share) ** goals
    return float(np.clip(np.mean(1.0 - no_sub_goal), 0.0, 1.0))


def allocate_player_prob(
    outcome: MatchOutcome, team_idx: int, stat: str, share: float, exposure: float,
    comparator: str, threshold: float, half: str = "full", include_et: bool = True,
) -> float:
    """P(player meets the line) as a share of the simulated team total (binomial-conditional)."""
    from scipy import stats  # noqa: PLC0415 - heavy import only on the gated player path

    if half in ("1H", "2H"):
        team = outcome.team_half(stat, team_idx, H1 if half == "1H" else H2)
    else:
        team = outcome.team_total(stat, team_idx, include_et=include_et)
    team = np.asarray(team)
    p = float(np.clip(share * exposure, 0.0, 1.0))
    k = float(threshold)
    if comparator == ">=":
        vals = stats.binom.sf(k - 1, team, p)
    elif comparator == ">":
        vals = stats.binom.sf(k, team, p)
    elif comparator == "<=":
        vals = stats.binom.cdf(k, team, p)
    elif comparator == "<":
        vals = stats.binom.cdf(k - 1, team, p)
    elif comparator == "==":
        vals = stats.binom.pmf(k, team, p)
    else:
        raise ValueError(f"unknown comparator {comparator!r}")
    return float(np.clip(np.mean(vals), 0.0, 1.0))


def _player_position_exposure(params: dict, ctx, settings: Settings) -> tuple[str, float]:
    """Position + expected exposure for the named player (mirrors the wheel's player-stat lookup)."""
    from sportspredict.markets.parser import player_name_match  # noqa: PLC0415

    name = params["player"]
    team_idx = _LABEL.get(params.get("team") or "A", TEAM_A)
    for p in ctx.lineup_for(team_idx):
        if player_name_match(name, p.name):
            return p.position, max(p.start_prob, (p.expected_minutes or 0.0) / 90.0)
    squads = ctx.extra.get("squads", {}) if ctx.extra else {}
    label = params.get("team") or "A"
    best = max(squads.get(label, []), key=lambda p: player_name_match(name, p.name), default=None)
    if best is not None and player_name_match(name, best.name) > 0:
        return best.position, 1.0
    return "FW", 1.0  # unknown player: assume a starting attacker


def _team_name_key(value: str) -> set[str]:
    text = unicodedata.normalize("NFKD", value or "").encode("ascii", "ignore").decode().lower()
    words = {word for word in text.replace("-", " ").split() if word not in {"and", "islands"}}
    return words


def _player_team_index(params: dict, ctx, shares: PlayerShares | None) -> int:
    explicit = params.get("team")
    if explicit in _LABEL:
        return _LABEL[explicit]
    from sportspredict.markets.parser import player_name_match

    name = params["player"]
    for team_idx in (TEAM_A, TEAM_B):
        if any(player_name_match(name, player.name) for player in ctx.lineup_for(team_idx)):
            return team_idx
    historical = shares.team(name) if shares is not None else None
    if historical:
        target = _team_name_key(historical)
        scores = [len(target & _team_name_key(ctx.team_a)), len(target & _team_name_key(ctx.team_b))]
        if max(scores) > 0 and scores[0] != scores[1]:
            return TEAM_A if scores[0] > scores[1] else TEAM_B
    return TEAM_A


def resolve_player_stat_alloc(
    params: dict, outcome: MatchOutcome, ctx, shares: "PlayerShares | None",
    settings: Settings | None = None, *, include_et: bool = True,
) -> float | None:
    """Resolve a wheel PLAYER_STAT spec via the allocation layer, or ``None`` to defer to the wheel."""
    settings = settings or default_settings()
    stat = params["stat"]
    team_idx = _player_team_index(params, ctx, shares)
    lookup_params = {**params, "team": "A" if team_idx == TEAM_A else "B"}
    position, exposure = _player_position_exposure(lookup_params, ctx, settings)
    share = shares.get(params["player"], stat) if shares is not None else None
    if share is None:
        share = position_prior_share(position, stat, settings)
    if share is None:
        return None  # no prior for this stat -> let the wheel handle it
    return allocate_player_prob(
        outcome, team_idx, stat, share, exposure,
        params["comparator"], params["threshold"], params.get("half", "full"),
        include_et=include_et,
    )


def resolve_player_goal_alloc(
    params: dict, outcome: MatchOutcome, ctx, shares: "PlayerShares | None",
    settings: Settings | None = None, *, include_assist: bool = False,
    include_et: bool = False, own_goal_share: float = 0.0,
) -> float:
    """Named scorer/goal-involvement probability tied to the simulated team goal total."""
    settings = settings or default_settings()
    team_idx = _player_team_index(params, ctx, shares)
    lookup_params = {**params, "team": "A" if team_idx == TEAM_A else "B"}
    position, exposure = _player_position_exposure(lookup_params, ctx, settings)
    goal_share = shares.get(params["player"], "goals") if shares is not None else None
    if goal_share is None:
        goal_share = position_prior_share(position, "goals", settings) or 0.0
    per_goal = float(goal_share) * exposure * (1.0 - float(own_goal_share))
    if include_assist:
        assist_share = shares.get(params["player"], "assists") if shares is not None else None
        if assist_share is None:
            assist_share = position_prior_share(position, "assists", settings) or 0.0
        per_goal += (
            float(assist_share) * exposure
            * float(settings.players.get("prob_goal_assisted", 0.70))
            * (1.0 - float(own_goal_share))
        )
    per_goal = float(np.clip(per_goal, 0.0, 0.95))
    half = params.get("half", "full")
    if half in ("1H", "2H"):
        goals = outcome.goals_half(team_idx, H1 if half == "1H" else H2)
    else:
        goals = outcome.goals_team(team_idx, include_et=include_et)
    return float(np.mean(1.0 - (1.0 - per_goal) ** np.asarray(goals, dtype=int)))
