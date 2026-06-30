"""Resolvers: reduce a :class:`MatchOutcome` to a probability for one :class:`MarketSpec`.

Each resolver is a pure function of the simulated draws plus the resolution rules in
``config/market_rules.yaml``. Because every market reads the *same* simulated matches,
their probabilities are mutually consistent and correlated by construction.
"""

from __future__ import annotations

import numpy as np

from ..config import Settings, default_settings
from ..features.context import MatchContext, PlayerInfo
from ..model.outcome import MatchOutcome
from ..model.players import prob_score, prob_score_or_assist
from ..types import GOALS, H1, H2, RESULT_A, RESULT_B, RESULT_DRAW, TEAM_A, TEAM_B
from .parser import player_name_match
from .schema import MarketSpec, MarketType, apply_comparator

_SIDE_TO_CODE = {"A": RESULT_A, "B": RESULT_B, "draw": RESULT_DRAW}
_LABEL_TO_IDX = {"A": TEAM_A, "B": TEAM_B}


def _counts_include_et(ctx: MatchContext, settings: Settings) -> bool:
    return bool(settings.market_rules.get("include_extra_time_in_counts", True)) and ctx.is_knockout


def _team_stat(outcome: MatchOutcome, stat: str, team: int, half: str, include_et: bool) -> np.ndarray:
    """Per-team value of a (possibly virtual) statistic for a half scope."""
    if stat == "cards":
        yellows = _team_stat(outcome, "yellows", team, half, include_et)
        if half == "full":
            return yellows + outcome.reds[team]
        return yellows  # red cards are not resolved by half
    if half == "full":
        return outcome.team_total(stat, team, include_et=include_et)
    if half == "2H":
        return outcome.team_half(stat, team, H2)
    if half == "1H":
        return outcome.team_half(stat, team, H1)
    raise ValueError(f"unknown half scope {half!r}")


def _stat_value(
    outcome: MatchOutcome, stat: str, scope: str, team: int | None, half: str, include_et: bool
) -> np.ndarray:
    if scope == "team":
        return _team_stat(outcome, stat, team if team is not None else TEAM_A, half, include_et)
    return _team_stat(outcome, stat, TEAM_A, half, include_et) + _team_stat(
        outcome, stat, TEAM_B, half, include_et
    )


def resolve(
    spec: MarketSpec,
    outcome: MatchOutcome,
    ctx: MatchContext,
    settings: Settings | None = None,
) -> float:
    settings = settings or default_settings()
    fn = _RESOLVERS[spec.market]
    p = fn(spec, outcome, ctx, settings)
    return float(np.clip(p, 0.0, 1.0))


# -- individual resolvers ---------------------------------------------------
def _resolve_match_result(spec, outcome, ctx, settings) -> float:
    side = spec.params["side"]
    # "win in regulation" / "draw after 90" resolve on regulation goals, not the official
    # extra-time/shootout outcome (which is used only for progression questions).
    if spec.params.get("regulation", False):
        reg_a = outcome.goals_team(TEAM_A, include_et=False)
        reg_b = outcome.goals_team(TEAM_B, include_et=False)
        if spec.params.get("double_chance", False):
            win = reg_a > reg_b if side == "A" else reg_b > reg_a
            return float(np.mean(win | (reg_a == reg_b)))
        if side == "A":
            return float(np.mean(reg_a > reg_b))
        if side == "B":
            return float(np.mean(reg_b > reg_a))
        return float(np.mean(reg_a == reg_b))  # draw / level after 90'
    code = _SIDE_TO_CODE[side]
    return float(np.mean(outcome.result == code))


def _resolve_total_goals(spec, outcome, ctx, settings) -> float:
    half = spec.params.get("half", "full")
    if half == "1H":
        tg = outcome.match_goals_half(H1)
    elif half == "2H":
        tg = outcome.match_goals_half(H2)
    else:
        tg = outcome.match_total(GOALS, include_et=_counts_include_et(ctx, settings))
    return float(np.mean(apply_comparator(tg, spec.params["comparator"], spec.params["threshold"])))


def _resolve_btts(spec, outcome, ctx, settings) -> float:
    half = spec.params.get("half", "full")
    if half in ("1H", "2H"):
        h = H1 if half == "1H" else H2
        a = outcome.goals_half(TEAM_A, h) >= 1
        b = outcome.goals_half(TEAM_B, h) >= 1
    else:
        include_et = _counts_include_et(ctx, settings)
        a = outcome.goals_team(TEAM_A, include_et=include_et) >= 1
        b = outcome.goals_team(TEAM_B, include_et=include_et) >= 1
    p = float(np.mean(a & b))
    return p if spec.params.get("yes", True) else 1.0 - p


def _resolve_team_vs_team_more(spec, outcome, ctx, settings) -> float:
    stat = spec.params["stat"]
    half = spec.params["half"]
    subject = _LABEL_TO_IDX[spec.params["subject"]]
    other = TEAM_B if subject == TEAM_A else TEAM_A
    include_et = _counts_include_et(ctx, settings) if half == "full" else False
    a = _team_stat(outcome, stat, subject, half, include_et)
    b = _team_stat(outcome, stat, other, half, include_et)
    tie_rule = settings.market_rules.get("team_vs_team_more", {}).get("tie_rule", "strict_no")
    p_more = float(np.mean(a > b))
    if tie_rule == "split":
        p_more += 0.5 * float(np.mean(a == b))
    return p_more


def _resolve_count_threshold(spec, outcome, ctx, settings) -> float:
    include_et = _counts_include_et(ctx, settings) if spec.params["half"] == "full" else False
    scope = spec.params["scope"]
    comparator, threshold = spec.params["comparator"], spec.params["threshold"]
    if scope in ("each_team", "either_team"):
        # "both teams to have N+ X" is a per-team conjunction, NOT the match total.
        a = _team_stat(outcome, spec.params["stat"], TEAM_A, spec.params["half"], include_et)
        b = _team_stat(outcome, spec.params["stat"], TEAM_B, spec.params["half"], include_et)
        ma = apply_comparator(a, comparator, threshold)
        mb = apply_comparator(b, comparator, threshold)
        return float(np.mean((ma & mb) if scope == "each_team" else (ma | mb)))
    team = _LABEL_TO_IDX.get(spec.params.get("team")) if spec.params.get("team") else None
    vals = _stat_value(outcome, spec.params["stat"], scope, team, spec.params["half"], include_et)
    return float(np.mean(apply_comparator(vals, comparator, threshold)))


def _resolve_half_conditional(spec, outcome, ctx, settings) -> float:
    sub = spec.params["subtype"]
    if sub == "more_goals_2h":
        return float(np.mean(outcome.match_goals_half(H2) > outcome.match_goals_half(H1)))
    if sub == "goal_in_half":
        half = H1 if spec.params["half"] == "1H" else H2
        return float(np.mean(outcome.match_goals_half(half) >= 1))
    if sub == "team_goal_in_half":
        team = _LABEL_TO_IDX[spec.params["team"]]
        half = H1 if spec.params["half"] == "1H" else H2
        return float(np.mean(outcome.goals_half(team, half) >= 1))
    if sub == "team_scores_both_halves":
        team = _LABEL_TO_IDX[spec.params["team"]]
        return float(np.mean((outcome.goals_half(team, H1) >= 1) & (outcome.goals_half(team, H2) >= 1)))
    if sub == "goal_in_both_halves":
        return float(np.mean((outcome.match_goals_half(H1) >= 1) & (outcome.match_goals_half(H2) >= 1)))
    if sub == "halftime_tied":
        return float(np.mean(outcome.goals_half(TEAM_A, H1) == outcome.goals_half(TEAM_B, H1)))
    if sub == "halftime_lead":
        team = _LABEL_TO_IDX[spec.params["team"]]
        other = TEAM_B if team == TEAM_A else TEAM_A
        return float(np.mean(outcome.goals_half(team, H1) > outcome.goals_half(other, H1)))
    raise ValueError(f"unknown half-conditional subtype {sub!r}")


def _squad_lookup(ctx, team_label: str, name: str) -> PlayerInfo | None:
    """Find a player in the (enricher-supplied) tournament squad for one team."""
    squads = ctx.extra.get("squads", {}) if ctx.extra else {}
    best = max(squads.get(team_label, []), key=lambda p: player_name_match(name, p.name), default=None)
    if best is not None and player_name_match(name, best.name) > 0:
        return best
    return None


def _player_target(spec, ctx):
    name = spec.params["player"]
    team_label = spec.params.get("team") or "A"
    team_idx = _LABEL_TO_IDX[team_label]
    lineup = ctx.lineup_for(team_idx)
    target = max(lineup, key=lambda p: player_name_match(name, p.name), default=None)
    if target is None or player_name_match(name, target.name) == 0:
        squad = _squad_lookup(ctx, team_label, name)
        position = squad.position if squad is not None else "FW"  # else assume an attacker
        target = PlayerInfo(name=name, team=team_label, position=position)
        lineup = None
    return team_idx, target, lineup or None


def _resolve_player(spec, outcome, ctx, settings) -> float:
    team_idx, target, lineup = _player_target(spec, ctx)
    return prob_score_or_assist(
        outcome, team_idx, target, lineup=lineup,
        half=spec.params.get("half", "full"), settings=settings,
    )


def _resolve_player_score(spec, outcome, ctx, settings) -> float:
    team_idx, target, lineup = _player_target(spec, ctx)
    return prob_score(
        outcome, team_idx, target, lineup=lineup,
        half=spec.params.get("half", "full"), settings=settings,
    )


def _resolve_player_stat(spec, outcome, ctx, settings) -> float:
    """Crude player count prop (e.g. shots on target): Poisson with a position-based mean.

    Independent of the match sim — a rough prior, but vastly better than mis-resolving a
    player prop as a match total. Uses the lineup position when known, else assumes a forward.
    """
    from scipy import stats

    name = spec.params["player"]
    team_label = spec.params.get("team") or "A"
    team_idx = _LABEL_TO_IDX.get(team_label, TEAM_A)
    pos, exposure = None, 1.0
    for p in ctx.lineup_for(team_idx):
        if player_name_match(name, p.name):
            pos = p.position
            # A confirmed bench player plays a fraction of the match: scale the rate by
            # expected exposure (63% 1+ SoT for a sub was the starter prior, far too high).
            exposure = max(p.start_prob, (p.expected_minutes or 0.0) / 90.0)
            break
    if pos is None:
        squad = _squad_lookup(ctx, team_label, name)
        pos = squad.position if squad is not None else "FW"
    lam_cfg = settings.raw.get("player_stat_lambda", {}).get(spec.params["stat"], {})
    lam = float(lam_cfg.get(pos, lam_cfg.get("FW", 0.6))) * exposure
    half = spec.params.get("half", "full")
    if half in ("1H", "2H"):
        share = float(settings.half_share_h1.get(spec.params["stat"], 0.5))
        lam *= share if half == "1H" else 1.0 - share
    ks = np.arange(0, 30)
    pmf = stats.poisson.pmf(ks, lam)
    mask = apply_comparator(ks, spec.params["comparator"], spec.params["threshold"])
    return float(pmf[np.asarray(mask, dtype=bool)].sum())


def _resolve_penalty_or_red(spec, outcome, ctx, settings) -> float:
    any_pen = outcome.penalties >= 1
    any_red = outcome.reds.sum(axis=0) >= 1
    return float(np.mean(any_pen | any_red))


def _resolve_penalty_awarded(spec, outcome, ctx, settings) -> float:
    return float(np.mean(outcome.penalties >= 1))


def _resolve_btts_and_total(spec, outcome, ctx, settings) -> float:
    """Compound: both teams score AND the match total meets the threshold."""
    include_et = _counts_include_et(ctx, settings)
    a = outcome.goals_team(TEAM_A, include_et=include_et) >= 1
    b = outcome.goals_team(TEAM_B, include_et=include_et) >= 1
    tg = outcome.match_total(GOALS, include_et=include_et)
    total_ok = apply_comparator(tg, spec.params["comparator"], spec.params["threshold"])
    return float(np.mean(a & b & total_ok))


def _resolve_win_to_nil(spec, outcome, ctx, settings) -> float:
    # A result-flavoured market: "win in regulation" convention, so 90' goals only.
    team = _LABEL_TO_IDX[spec.params["team"]]
    other = TEAM_B if team == TEAM_A else TEAM_A
    a = outcome.goals_team(team, include_et=False)
    b = outcome.goals_team(other, include_et=False)
    return float(np.mean((a > b) & (b == 0)))


def _resolve_clean_sheet(spec, outcome, ctx, settings) -> float:
    # A count-flavoured market ("concede 0 goals"), so it follows the knockout ET rule.
    team = _LABEL_TO_IDX[spec.params["team"]]
    other = TEAM_B if team == TEAM_A else TEAM_A
    conceded = outcome.goals_team(other, include_et=_counts_include_et(ctx, settings))
    return float(np.mean(conceded == 0))


def _resolve_goes_to_et(spec, outcome, ctx, settings) -> float:
    # et_played is exactly "tied after 90' in a knockout"; all-False for group games.
    return float(np.mean(outcome.et_played))


def _resolve_goes_to_shootout(spec, outcome, ctx, settings) -> float:
    et_a = outcome.et_counts[GOALS][TEAM_A]
    et_b = outcome.et_counts[GOALS][TEAM_B]
    return float(np.mean(outcome.et_played & (et_a == et_b)))


def _resolve_total_goals_parity(spec, outcome, ctx, settings) -> float:
    half = spec.params.get("half", "full")
    if half == "1H":
        tg = outcome.match_goals_half(H1)
    elif half == "2H":
        tg = outcome.match_goals_half(H2)
    else:
        tg = outcome.match_total(GOALS, include_et=_counts_include_et(ctx, settings))
    want = 1 if spec.params["parity"] == "odd" else 0
    return float(np.mean(tg % 2 == want))


_RESOLVERS = {
    MarketType.MATCH_RESULT: _resolve_match_result,
    MarketType.TOTAL_GOALS: _resolve_total_goals,
    MarketType.BTTS: _resolve_btts,
    MarketType.TEAM_VS_TEAM_MORE: _resolve_team_vs_team_more,
    MarketType.COUNT_THRESHOLD: _resolve_count_threshold,
    MarketType.HALF_CONDITIONAL: _resolve_half_conditional,
    MarketType.PLAYER_SCORE_OR_ASSIST: _resolve_player,
    MarketType.PLAYER_SCORE: _resolve_player_score,
    MarketType.PLAYER_STAT: _resolve_player_stat,
    MarketType.PENALTY_OR_RED: _resolve_penalty_or_red,
    MarketType.PENALTY_AWARDED: _resolve_penalty_awarded,
    MarketType.BTTS_AND_TOTAL: _resolve_btts_and_total,
    MarketType.WIN_TO_NIL: _resolve_win_to_nil,
    MarketType.CLEAN_SHEET: _resolve_clean_sheet,
    MarketType.GOES_TO_ET: _resolve_goes_to_et,
    MarketType.GOES_TO_SHOOTOUT: _resolve_goes_to_shootout,
    MarketType.TOTAL_GOALS_PARITY: _resolve_total_goals_parity,
}
