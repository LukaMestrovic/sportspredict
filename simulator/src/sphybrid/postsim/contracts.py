"""Stable evidence keys shared by the simulator report and historical validation."""

from __future__ import annotations


def _number(value) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "na"
    return str(int(number)) if number.is_integer() else f"{number:g}"


def _count_threshold(comparator, threshold) -> tuple[str, str]:
    """Canonicalize equivalent integer count thresholds for stable evidence keys."""
    try:
        number = float(threshold)
    except (TypeError, ValueError):
        return str(comparator), _number(threshold)
    if (
        str(comparator) == ">="
        and number.is_integer()
        and number > 0
    ):
        return ">", _number(number - 1)
    return str(comparator), _number(number)


def contract_key(market: str, params: dict | None, *, stage: str | None = None) -> str:
    params = params or {}
    market = str(market)
    regulation_only = bool(params.get("regulation_scope") or params.get("regulation"))
    # With no extra-time branch, "match" and "regulation" are the same observable contract.
    # Pool them so group-stage evidence is usable for either wording, while knockout questions
    # remain separated unless they explicitly say regulation.
    if stage is not None and str(stage).lower() != "knockout":
        regulation_only = True
    regulation = "reg" if regulation_only else "match"
    if market == "goal_window":
        window = params.get("window", "unknown")
        if window == "stoppage":
            return f"goal_window:stoppage:{params.get('half', 'unknown')}"
        if window == "stoppage_any":
            return "goal_window:stoppage:any:reg"
        return f"goal_window:{window}:{'et' if params.get('include_et') else 'reg'}"
    if market == "first_card_before_first_goal":
        return "first_card_before_first_goal:reg"
    if market in {"card_window", "stat_window"}:
        stat = params.get("stat", "cards" if market == "card_window" else "event")
        return (
            f"{market}:{stat}:{params.get('window', 'unknown')}:"
            f"{'et' if params.get('include_et') else 'reg'}:"
            f"{params.get('comparator', '>=')}:{_number(params.get('threshold', 1))}"
        )
    if market == "first_goal":
        period = params.get("half") or "full"
        scope = ":et" if period == "full" and params.get("include_et") else ""
        return f"first_goal:{period}{scope}:team"
    if market == "first_goal_half":
        return f"first_goal_half:{params.get('half', 'unknown')}:reg"
    if market == "compound_and":
        return "compound:first_goal_and_other_team_scores_2h"
    if market == "team_corners_and_total_shots_more":
        return "compound:team_more_corners_and_total_shots:reg"
    if market == "any_player_threshold" and params.get("stat") == "goals":
        comparator, threshold = _count_threshold(
            params.get("comparator"), params.get("threshold"),
        )
        return f"{market}:goals:{comparator}:{threshold}:reg"
    if market in {"any_player_threshold", "total_shots_threshold"}:
        return (
            f"{market}:{params.get('stat', 'shots_total')}:"
            f"{params.get('comparator')}:{_number(params.get('threshold'))}:reg"
        )
    if market in {"substitute_score", "substitute_score_or_assist",
                  "substitution_before_halftime", "red_card",
                  "both_teams_card", "win_margin", "team_score_no_own",
                  "lead_any_time", "cards_more_than_goals", "player_full_match",
                  "win_both_halves", "exact_goal_margin"}:
        if market == "substitution_before_halftime":
            regulation = "reg"
        if market == "lead_any_time":
            regulation = "match" if params.get("include_et") else "reg"
        if market == "player_full_match":
            regulation = "reg"
        suffix = f":{_number(params.get('threshold'))}" if market == "win_margin" else ""
        if market == "exact_goal_margin":
            suffix = f":{_number(params.get('margin'))}"
        if market == "player_full_match":
            suffix = ":player"
        return f"{market}:{regulation}{suffix}"
    if market in {"player_score", "player_score_or_assist"}:
        scope = "reg" if params.get("half", "full") in {"1H", "2H"} else regulation
        return f"{market}:{params.get('half', 'full')}:{scope}:player"
    if market == "player_stat":
        scope = "reg" if params.get("half", "full") in {"1H", "2H"} else regulation
        return (
            f"player_stat:{params.get('stat')}:{params.get('half', 'full')}:"
            f"{params.get('comparator')}:{_number(params.get('threshold'))}:{scope}:player"
        )
    if market == "count_threshold":
        scope = "team" if params.get("scope") == "team" else params.get("scope", "match")
        time_scope = "reg" if params.get("half", "full") in {"1H", "2H"} else regulation
        return (
            f"count:{params.get('stat')}:{scope}:{params.get('half', 'full')}:"
            f"{params.get('comparator')}:{_number(params.get('threshold'))}:{time_scope}"
        )
    if market == "team_vs_team_more":
        scope = "reg" if params.get("half", "full") in {"1H", "2H"} else regulation
        return f"compare:{params.get('stat')}:{params.get('half', 'full')}:{scope}"
    if market == "total_goals":
        scope = "reg" if params.get("half", "full") in {"1H", "2H"} else regulation
        return (
            f"total_goals:{params.get('half', 'full')}:{params.get('comparator')}:"
            f"{_number(params.get('threshold'))}:{scope}"
        )
    if market == "match_result":
        side = "draw" if params.get("side") == "draw" else "team"
        kind = "double_chance" if params.get("double_chance") else side
        return f"match_result:{kind}:{'reg' if params.get('regulation', True) else 'advance'}"
    if market == "btts":
        scope = "reg" if params.get("half", "full") in {"1H", "2H"} else regulation
        return f"btts:{params.get('half', 'full')}:{scope}"
    if market == "half_conditional":
        return f"half_conditional:{params.get('subtype')}"
    if market in {"penalty_awarded", "penalty_or_red", "clean_sheet", "btts_and_total"}:
        return f"{market}:{regulation}"
    return f"{market}:{regulation}"


def question_contract_key(question: str, ctx) -> str:
    """Return the report key for an exact question without running simulations."""
    from sportspredict.markets import parse_question

    from .markets import REGULATION_STANDARD, parse_extended

    extended = parse_extended(question, ctx)
    if extended is not None:
        if extended.market == REGULATION_STANDARD:
            baseline = extended.params["baseline_spec"]
            params = {
                **baseline.params,
                "regulation_scope": bool(extended.params.get("regulation", False)),
            }
            return contract_key(baseline.market.value, params, stage=ctx.stage)
        return contract_key(extended.market, extended.params, stage=ctx.stage)
    baseline = parse_question(question, ctx)
    return contract_key(baseline.market.value, baseline.params, stage=ctx.stage)
