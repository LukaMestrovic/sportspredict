"""Post-simulation player count-prop allocation.

The baseline prices a player count prop (e.g. "<player> 2+ shots on target") with a standalone
position-Poisson that is *independent* of the match simulation, so the player market and the team
market for the same statistic can drift apart. The allocation layer makes the player's count a
**share of the same simulated team total**, keeping player and team markets coherent.

Given the team's simulated total ``T`` for a statistic (an array over worlds) and a per-unit
ownership probability ``p = share * exposure``, the player's count is ``Binomial(T, p)`` conditional
on ``T`` — the natural generalisation of the baseline's score-or-assist estimator (which uses the
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
from sportspredict.types import GOALS, H1, H2, TEAM_A, TEAM_B

_LABEL = {"A": TEAM_A, "B": TEAM_B}
SUBSTITUTE_INVOLVEMENT_GOAL_MODEL = {
    # Fitted on 2,702 API-Football labelable matches in the five-substitute era
    # (2020-05-08 onward). The curve is:
    #   p(lambda) = 1 - exp(-beta * lambda)
    # where lambda is total expected regulation goals. Its fitted average
    # prediction over the training rows is 0.462987, centered on the 0.460770
    # empirical rate while preserving the required p(0 goals)=0 behavior.
    #
    # WC2026 has been materially hotter so far for this contract, but the sample
    # is still small. The tournament layer treats historical all-era evidence as
    # 300 effective observations and shifts the goal curve toward the current
    # WC2026 rate on the logit scale, preserving the xG ordering.
    "beta": 0.261798762,
    "empirical_rate": 0.4607698001480385,
    "mean_goals": 2.7472242783123613,
    "mean_prediction": 0.462987,
    "observations": 2702,
    "tournament_shrinkage": {
        "enabled": True,
        "competition": "wc2026",
        "target_kickoff": "2026-07-09T20:00:00+00:00",
        "observations": 90,
        "yes_events": 48,
        "historical_effective_observations": 300,
    },
}


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


def _prob_binomial_strict_more(
    left_total: int,
    right_total: int,
    left_probability: float,
    right_probability: float,
) -> float:
    """Exact ``P(X > Y)`` for two conditionally independent binomials.

    The two players are on opposing teams, so their allocations are independent
    conditional on the two team totals in one shared simulation world. Ties are
    NO, matching the literal ``more than`` contract.
    """
    from scipy import stats

    left_total = max(int(left_total), 0)
    right_total = max(int(right_total), 0)
    left_probability = float(np.clip(left_probability, 0.0, 1.0))
    right_probability = float(np.clip(right_probability, 0.0, 1.0))
    values = np.arange(left_total + 1)
    probability = np.sum(
        stats.binom.pmf(values, left_total, left_probability)
        * stats.binom.cdf(values - 1, right_total, right_probability)
    )
    return float(np.clip(probability, 0.0, 1.0))


def _prob_distinct_at_least(total: int, probs: np.ndarray, threshold: int) -> float:
    """Exact multinomial occupancy probability ``P(distinct owners >= K)``.

    Conditional on a team's total attempts, every shot is allocated to one
    player using ``probs``. The exponential generating function

    ``prod_i [1 + y * (exp(p_i*x) - 1)]``

    has coefficient ``x^total y^d * total!`` equal to the probability of
    exactly ``d`` distinct shooters. Dynamic programming evaluates those
    coefficients without another Monte Carlo layer.
    """
    import math

    total = max(int(total), 0)
    threshold = int(threshold)
    if threshold <= 0:
        return 1.0
    if total < threshold:
        return 0.0
    probabilities = np.asarray(probs, dtype=float)
    probabilities = np.where(np.isfinite(probabilities), np.maximum(probabilities, 0.0), 0.0)
    if probabilities.size < threshold or probabilities.sum() <= 0:
        return 0.0
    probabilities /= probabilities.sum()

    players = len(probabilities)
    coefficients = np.zeros((players + 1, total + 1), dtype=float)
    coefficients[0, 0] = 1.0
    used_players = 0
    for probability in probabilities:
        updated = coefficients.copy()  # this player owns zero attempts
        positive = np.asarray([
            float(probability) ** count / math.factorial(count)
            for count in range(1, total + 1)
        ])
        for distinct in range(used_players + 1):
            base = coefficients[distinct]
            for already_used in np.flatnonzero(base):
                room = total - int(already_used)
                if room <= 0:
                    continue
                updated[distinct + 1, already_used + 1:total + 1] += (
                    base[already_used] * positive[:room]
                )
        coefficients = updated
        used_players += 1
    probability = math.factorial(total) * coefficients[threshold:, total].sum()
    return float(np.clip(probability, 0.0, 1.0))


def _player_allocation_probability(
    player: str,
    team_idx: int,
    stat: str,
    ctx,
    shares: PlayerShares | None,
    settings: Settings,
) -> float:
    label = "A" if team_idx == TEAM_A else "B"
    position, exposure = _player_position_exposure(
        {"player": player, "team": label}, ctx, settings,
    )
    share = shares.get(player, stat) if shares is not None else None
    if share is None:
        share = position_prior_share(position, stat, settings) or 0.0
    return float(np.clip(float(share) * float(exposure), 0.0, 1.0))


def prob_player_stat_more(
    outcome: MatchOutcome,
    ctx,
    *,
    stat: str,
    left_player: str,
    left_team: int,
    right_player: str,
    right_team: int,
    shares: PlayerShares | None,
    settings: Settings,
) -> float:
    """Strict two-player comparison tied to both simulated regulation totals."""
    if left_team == right_team:
        raise ValueError("same-team player comparisons require multinomial allocation")
    left_p = _player_allocation_probability(
        left_player, left_team, stat, ctx, shares, settings,
    )
    right_p = _player_allocation_probability(
        right_player, right_team, stat, ctx, shares, settings,
    )
    left_totals = np.asarray(
        outcome.team_total(stat, left_team, include_et=False), dtype=int,
    )
    right_totals = np.asarray(
        outcome.team_total(stat, right_team, include_et=False), dtype=int,
    )
    lookup = {
        (int(left), int(right)): _prob_binomial_strict_more(
            int(left), int(right), left_p, right_p,
        )
        for left, right in np.unique(
            np.column_stack((left_totals, right_totals)), axis=0,
        )
    }
    values = np.asarray([
        lookup[(int(left), int(right))]
        for left, right in zip(left_totals, right_totals)
    ])
    return float(np.clip(np.mean(values), 0.0, 1.0))


def prob_team_unique_shooters(
    total_shots: np.ndarray,
    ctx,
    team_idx: int,
    threshold: int,
    shares: PlayerShares | None,
    settings: Settings,
) -> float:
    """Probability a team has ``threshold`` distinct regulation shot takers.

    Team total shots come from the simulator's shots-on-target plus fitted
    off-target model. The current compact player artifact has shots-on-target
    ownership shares, which are used as disclosed shot-attempt allocation
    weights; expected-minutes exposure is retained and the vector is
    renormalized across the available lineup.
    """
    probs, _ = _team_share_vector(
        ctx, team_idx, "shots_on_target", shares, settings,
    )
    totals = np.asarray(total_shots, dtype=int)
    lookup = {
        int(total): _prob_distinct_at_least(int(total), probs, threshold)
        for total in np.unique(totals)
    }
    values = np.asarray([lookup[int(total)] for total in totals])
    return float(np.clip(np.mean(values), 0.0, 1.0))


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
    *, fallback_share: float, own_goal_share: float = 0.0,
) -> float:
    """Probability a bench player scores, conditional on the simulated regulation goal totals."""
    no_sub_goal = np.ones(outcome.n_sims, dtype=float)
    for team_idx in (TEAM_A, TEAM_B):
        probs, bench = _team_share_vector(ctx, team_idx, "goals", shares, settings)
        sub_share = float(probs[bench].sum()) if bench.any() else float(fallback_share)
        sub_share *= 1.0 - float(own_goal_share)
        sub_share = float(np.clip(sub_share, 0.01, 0.65))
        goals = np.asarray(outcome.goals_team(team_idx, include_et=False), dtype=int)
        no_sub_goal *= (1.0 - sub_share) ** goals
    return float(np.clip(np.mean(1.0 - no_sub_goal), 0.0, 1.0))


def prob_substitute_goal_involvement(
    outcome: MatchOutcome, ctx, shares: PlayerShares | None, settings: Settings,
    *,
    fallback_goal_share: float,
    fallback_assist_share: float,
    own_goal_share: float = 0.0,
) -> float:
    """Probability a substitute scores or assists, conditional on expected regulation goals."""
    del shares, settings, fallback_goal_share, fallback_assist_share, own_goal_share
    expected_goals = float(np.mean(outcome.match_total(GOALS, include_et=False)))
    calibration = _substitute_involvement_calibration_for_context(ctx)
    return prob_substitute_goal_involvement_from_expected_goals(
        expected_goals, calibration=calibration,
    )


def prob_substitute_goal_involvement_from_expected_goals(
    expected_total_goals: float,
    *,
    beta: float = SUBSTITUTE_INVOLVEMENT_GOAL_MODEL["beta"],
    calibration: dict | None = None,
) -> float:
    """Five-sub-era P(substitute score or assist) from total expected regulation goals."""
    lam = max(float(expected_total_goals), 0.0)
    beta = max(float(beta), 1e-9)
    probability = 1.0 - np.exp(-beta * lam)
    if calibration is not None:
        probability = _apply_substitute_involvement_shrinkage(
            float(probability), calibration=calibration,
        )
    return float(np.clip(probability, 0.0, 0.95))


def _substitute_involvement_calibration_for_context(ctx) -> dict | None:
    if ctx is not None and getattr(ctx, "extra", None):
        calibration = ctx.extra.get("substitute_score_or_assist_calibration")
        if calibration is not None:
            return calibration

    calibration = SUBSTITUTE_INVOLVEMENT_GOAL_MODEL.get("tournament_shrinkage")
    if not calibration or calibration.get("enabled") is False:
        return None
    kickoff = str(getattr(ctx, "date", "") or "")
    target_kickoff = str(calibration.get("target_kickoff") or "")
    if kickoff and target_kickoff and kickoff >= target_kickoff:
        return calibration
    return None


def _apply_substitute_involvement_shrinkage(
    probability: float, *, calibration: dict | None = None,
) -> float:
    if not calibration or calibration.get("enabled") is False:
        return probability

    yes_events = calibration.get("yes_events", calibration.get("tournament_yes_events"))
    observations = calibration.get("observations", calibration.get("tournament_observations"))
    try:
        yes = float(yes_events)
        n = float(observations)
        effective_history = float(calibration.get(
            "historical_effective_observations",
            calibration.get("history_effective_observations", 300),
        ))
        historical_rate = float(calibration.get(
            "historical_rate", SUBSTITUTE_INVOLVEMENT_GOAL_MODEL["empirical_rate"],
        ))
        model_center = float(calibration.get(
            "model_center", SUBSTITUTE_INVOLVEMENT_GOAL_MODEL["mean_prediction"],
        ))
    except (TypeError, ValueError):
        return probability
    if n <= 0 or effective_history <= 0:
        return probability

    target_center = (historical_rate * effective_history + yes) / (effective_history + n)
    target_center = float(np.clip(target_center, 0.01, 0.99))
    model_center = float(np.clip(model_center, 0.01, 0.99))
    probability = float(np.clip(probability, 0.01, 0.99))
    return float(_inv_logit(_logit(probability) + _logit(target_center) - _logit(model_center)))


def _logit(probability: float) -> float:
    p = float(np.clip(probability, 1e-9, 1.0 - 1e-9))
    return float(np.log(p / (1.0 - p)))


def _inv_logit(value: float) -> float:
    return float(1.0 / (1.0 + np.exp(-float(value))))


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
    """Position + expected exposure for the named player (mirrors the baseline's player-stat lookup)."""
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
    """Resolve a baseline PLAYER_STAT spec via the allocation layer, or ``None`` to defer to the baseline."""
    settings = settings or default_settings()
    stat = params["stat"]
    team_idx = _player_team_index(params, ctx, shares)
    lookup_params = {**params, "team": "A" if team_idx == TEAM_A else "B"}
    position, exposure = _player_position_exposure(lookup_params, ctx, settings)
    share = shares.get(params["player"], stat) if shares is not None else None
    if share is None:
        share = position_prior_share(position, stat, settings)
    if share is None:
        return None  # no prior for this stat -> let the baseline handle it
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
