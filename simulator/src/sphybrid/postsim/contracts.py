"""Stable evidence keys shared by the simulator report and historical validation."""

from __future__ import annotations


def _number(value) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "na"
    return str(int(number)) if number.is_integer() else f"{number:g}"


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
        return f"goal_window:{window}:{'et' if params.get('include_et') else 'reg'}"
    if market in {"card_window", "stat_window"}:
        stat = params.get("stat", "cards" if market == "card_window" else "event")
        return (
            f"{market}:{stat}:{params.get('window', 'unknown')}:"
            f"{'et' if params.get('include_et') else 'reg'}:"
            f"{params.get('comparator', '>=')}:{_number(params.get('threshold', 1))}"
        )
    if market == "first_goal":
        return f"first_goal:{params.get('half') or 'full'}:team"
    if market == "compound_and":
        return "compound:first_goal_and_other_team_scores_2h"
    if market in {"any_player_threshold", "total_shots_threshold"}:
        return (
            f"{market}:{params.get('stat', 'shots_total')}:"
            f"{params.get('comparator')}:{_number(params.get('threshold'))}:reg"
        )
    if market in {"substitute_score", "substitution_before_halftime", "red_card",
                  "both_teams_card", "win_margin"}:
        if market == "substitution_before_halftime":
            regulation = "reg"
        suffix = f":{_number(params.get('threshold'))}" if market == "win_margin" else ""
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
            wheel = extended.params["wheel_spec"]
            params = {
                **wheel.params,
                "regulation_scope": bool(extended.params.get("regulation", False)),
            }
            return contract_key(wheel.market.value, params, stage=ctx.stage)
        return contract_key(extended.market, extended.params, stage=ctx.stage)
    wheel = parse_question(question, ctx)
    return contract_key(wheel.market.value, wheel.params, stage=ctx.stage)
