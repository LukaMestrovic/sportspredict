"""Market matcher: structured intent -> API-Football bet specification.

Maps a parsed question intent to (bet_id, target outcome) drawn from the
API-Football pre-match market catalog. Odds API markets are handled separately.
If no API-Football market fits, returns None and the question is skipped from
that provider path.

See ``soccer_live_odds_market_catalog.pdf`` for the raw provider catalog.

A spec is one of:
  {"type": "ou",     "bet_id": int, "side": "Over"|"Under", "line": float}
  {"type": "select", "bet_id": int, "value": str}            # de-vig full set
"""
from __future__ import annotations

# Team-specific over/under markets: subject -> (home bet id, away bet id)
_TEAM_OU = {
    "team_total_goals": (16, 17),     # Total - Home / Away
    "team_corners": (57, 58),         # Home/Away Corners Over/Under
    "team_cards": (82, 83),           # Home/Away Team Total Cards
    "team_offsides": (167, 168),      # Offsides Home/Away Total
    "team_fouls": (171, 170),         # Fouls. Home/Away Total
    "team_shots": (221, 220),         # Shots. Home/Away Total (on+off target)
}

# Match-level over/under markets: bet id
_MATCH_OU = {
    "total_goals": 5,      # Goals Over/Under
    "total_corners": 45,   # Corners Over Under
    "total_cards": 80,     # Cards Over/Under
    "total_offsides": 164, # Offsides Total
    "total_fouls": 173,    # Fouls. Total
    "total_shots_on_target": 87, # Total ShotOnGoal
    "total_shots": 211,    # Total Shots (on+off target)
}

# Team yes/no markets: subject -> (home bet id, away bet id), target value "Yes"
_TEAM_YESNO = {
    "team_score": (43, 44),       # Home/Away Team Score a Goal
    "team_score_1h": (114, 116),  # ... (1st Half)
    "team_score_2h": (115, 117),  # ... (2nd Half)
    "team_clean_sheet": (27, 28),         # Clean Sheet - Home/Away
    "team_score_both_halves": (111, 112), # Home/Away team will score in both halves
}

# Match-level yes/no select markets: bet id, target value "Yes"
_MATCH_YESNO = {
    "both_teams_card": 252,    # Both Teams to Receive a Card
    "penalty_awarded": 163,    # Penalty Awarded
}

# Two-way select markets that pick the named team: bet id, value Home/Away.
_TEAM_SELECT = {
    "to_advance": 61,          # To Qualify
}

# Team "more than opponent" 1x2 markets: subject picks Home/Away value
_COMPARE = {
    "corners_compare": 55,   # Corners 1x2
    "cards_compare": 158,    # Yellow Cards 1x2 (accepted proxy for all cards)
    "offsides_compare": 165, # Offsides 1x2
    "fouls_compare": 175,    # Fouls. 1x2
    "shots_on_target_compare": 176,  # ShotOnTarget 1x2
}

CARDS_COMPARE_PROXY = "yellow_cards_1x2_for_all_cards_compare"
CARDS_COMPARE_PROXY_NOTE = (
    "yellow-card/bookings team-most-cards 1x2 proxy for the all-cards "
    "comparison; red cards are rare but can change SportPredict settlement"
)
TEAM_SCORE_NO_OWN_GOALS_PROXY = "team_to_score_for_team_score_excluding_own_goals"
TEAM_SCORE_NO_OWN_GOALS_PROXY_NOTE = (
    "team-to-score scoreboard proxy for the excluding-own-goals contract; "
    "opponent own goals are rare but can change SportPredict settlement"
)

# Player markets — sourced from the Odds API (API-Football rarely quotes them).
_PLAYER_MARKETS = [
    "player_shots_on_target",  # over/under shots on target
    "player_goal_scorer",      # to score a goal (anytime)
    "player_score_or_assist",  # to score or assist
    "player_card",             # to be booked / receive a card
]

# The vocabulary the LLM parser is allowed to emit.
MARKET_KEYS = (
    ["match_winner", "match_draw", "btts", "highest_scoring_half_2h",
     "double_chance", "first_team_to_score", "win_margin", "red_card", "own_goal"]
    + list(_MATCH_OU)
    + list(_TEAM_OU)
    + list(_TEAM_YESNO)
    + list(_MATCH_YESNO)
    + list(_TEAM_SELECT)
    + list(_COMPARE)
    + _PLAYER_MARKETS
    + ["none"]
)


def _line_from_threshold(comparator: str, threshold) -> tuple[str, float] | None:
    """('gte', 2) -> ('Over', 1.5); ('lte', 2) -> ('Under', 2.5)."""
    if threshold is None:
        return None
    try:
        n = float(threshold)
    except (TypeError, ValueError):
        return None
    if comparator == "gte":
        return "Over", n - 0.5
    if comparator == "lte":
        return "Under", n + 0.5
    return None


# A total_* market whose subject is one team really means the team_* variant.
_TOTAL_TO_TEAM = {
    "total_offsides": "team_offsides",
    "total_corners": "team_corners",
    "total_cards": "team_cards",
    "total_fouls": "team_fouls",
    "total_goals": "team_total_goals",
    "total_shots_on_target": "team_shots_on_target",
    "total_shots": "team_shots",
}
# A team_* count with a "more than opponent" comparator means the 1x2 market.
_TEAM_TO_COMPARE = {
    "team_corners": "corners_compare",
    "team_offsides": "offsides_compare",
    "team_fouls": "fouls_compare",
    "team_shots_on_target": "shots_on_target_compare",
    "team_cards": "cards_compare",
}
_COMPARE_TO_TEAM = {value: key for key, value in _TEAM_TO_COMPARE.items()}


# Half-period bet IDs (API-Football). Used when a question is about 1st/2nd half.
_HALF_SELECT = {                                    # 1x2 select markets
    "match_winner": {"1H": 13, "2H": 3},            # First/Second Half Winner
    "match_draw": {"1H": 13, "2H": 3},              # Draw in the named half
    "corners_compare": {"1H": 130, "2H": 131},      # Corners 1x2 (1st/2nd Half)
}
_HALF_MATCH_OU = {                                  # match-level over/under
    "total_goals": {"1H": 6, "2H": 26},             # Goals O/U First/Second Half
    "total_corners": {"1H": 77, "2H": 127},         # Total Corners (1st/2nd Half)
    "total_cards": {"1H": 155, "2H": 156},          # Yellow O/U (1st/2nd Half)
}
_HALF_TEAM_OU = {                                   # team over/under
    "team_total_goals": {"1H": (105, 106), "2H": (107, 108)},
    "team_corners": {"1H": (132, 134), "2H": (133, 135)},
}
_HALF_BTTS = {"1H": 34, "2H": 35}                   # BTTS First/Second Half


def _normalize(market: str, subject: str, comp: str, period: str) -> str:
    """Repair common parser ambiguities before mapping to a bet."""
    if market in _TOTAL_TO_TEAM and subject in ("home", "away"):
        market = _TOTAL_TO_TEAM[market]
    # Corner 1x2 contracts also exist for each half. The other comparisons are
    # full-match only; cards_compare is a full-match yellow-card proxy.
    if (comp == "more" and market in _TEAM_TO_COMPARE
            and (period == "match" or market == "team_corners")):
        market = _TEAM_TO_COMPARE[market]
    # Repair the inverse parser ambiguity: a numeric count is a team total, not
    # a 1x2 comparison.
    if comp in ("gte", "lte") and market in _COMPARE_TO_TEAM:
        market = _COMPARE_TO_TEAM[market]
    if comp == "more" and period in ("1H", "2H") and market in _TEAM_YESNO:
        market = "match_winner"
    return market


def match_intent(
    intent: dict, home: str, away: str, *, stage: str | None = None,
) -> dict | None:
    market = intent.get("market")
    subject = intent.get("subject")
    comp = intent.get("comparator")
    threshold = intent.get("threshold")
    period = intent.get("period", "match")

    if not market or market == "none":
        return None
    market = _normalize(market, subject, comp, period)
    # Standard pre-match bookmaker contracts settle at 90 minutes. In a
    # knockout match they are not exact evidence for an unqualified full-match
    # question, which includes extra time. Qualification is exact; first-team-
    # to-score is the deliberate narrow proxy exception because ET-only first
    # goals are rare and the direct market is materially stronger evidence.
    if (
        str(stage or "").lower() == "knockout"
        and intent.get("time_scope") == "full_match"
        and period == "match"
        and market not in {"to_advance", "first_team_to_score"}
    ):
        return None
    if market == "highest_scoring_half_2h":
        period = "match"

    # Half-period bet IDs. If a 1st/2nd-half question has no half variant we
    # return None (the question cascades to the next source) rather than
    # mispricing it with a full-match line.
    half = period if period in ("1H", "2H") else None
    if half:
        sel = _HALF_SELECT.get(market, {}).get(half)
        mou = _HALF_MATCH_OU.get(market, {}).get(half)
        tou = _HALF_TEAM_OU.get(market, {}).get(half)
        if market == "btts":
            return {"type": "select", "bet_id": _HALF_BTTS[half], "value": "Yes",
                    "label": f"BTTS {half}"}
        if market == "match_draw":
            if sel is None:
                return None
            return {"type": "select", "bet_id": sel, "value": "Draw",
                    "label": f"draw {half}"}
        if market in _COMPARE or market == "match_winner":
            if sel is None or subject not in ("home", "away"):
                return None
            return {"type": "select", "bet_id": sel,
                    "value": "Home" if subject == "home" else "Away",
                    "label": f"{market} {subject} {half}"}
        if market in _MATCH_OU:
            ou = _line_from_threshold(comp, threshold)
            if mou is None or not ou:
                return None
            return {"type": "ou", "bet_id": mou, "side": ou[0], "line": ou[1],
                    "label": f"{market} {ou[0]} {ou[1]} {half}"}
        if market in _TEAM_OU:
            ou = _line_from_threshold(comp, threshold)
            if tou is None or not ou or subject not in ("home", "away"):
                return None
            return {"type": "ou", "bet_id": tou[0 if subject == "home" else 1],
                    "side": ou[0], "line": ou[1],
                    "label": f"{subject} {market} {ou[0]} {ou[1]} {half}"}
        if market not in _TEAM_YESNO:  # team_score_1h/2h handled below
            return None

    if market == "match_winner":
        if subject not in ("home", "away"):
            return None
        return {"type": "select", "bet_id": 1,
                "value": "Home" if subject == "home" else "Away",
                "label": f"{home if subject=='home' else away} win"}

    if market == "match_draw":
        return {"type": "select", "bet_id": 1, "value": "Draw",
                "label": "match draw"}

    if market == "first_team_to_score":
        if subject not in ("home", "away") or period != "match":
            return None
        spec = {"type": "select", "bet_id": 14,
                "value": "Home" if subject == "home" else "Away",
                "label": f"{subject} scores first"}
        if (str(stage or "").lower() == "knockout"
                and intent.get("time_scope") == "full_match"):
            spec["scope_proxy"] = "regulation_first_team_to_score_for_full_match"
        return spec

    if market == "btts":
        return {"type": "select", "bet_id": 8, "value": "Yes", "label": "Both teams score"}

    if market == "highest_scoring_half_2h":
        return {"type": "select", "bet_id": 11, "value": "2nd Half",
                "label": "2nd half outscores 1st"}

    if market in _TEAM_SELECT:
        if subject not in ("home", "away"):
            return None
        return {"type": "select", "bet_id": _TEAM_SELECT[market],
                "value": "Home" if subject == "home" else "Away",
                "label": f"{market} {subject}"}

    if market in _MATCH_YESNO:
        return {"type": "select", "bet_id": _MATCH_YESNO[market], "value": "Yes",
                "label": market}

    if market == "red_card":
        # "a red card shown" == total red cards >= 1 == Over 0.5.
        return {"type": "ou", "bet_id": 335, "side": "Over", "line": 0.5,
                "label": "red card shown"}

    if market == "own_goal":
        return {"type": "select", "bet_id": 59, "value": "Yes",
                "label": "own goal scored"}

    if market == "win_margin":
        if subject not in ("home", "away") or threshold is None:
            return None
        try:
            n = int(threshold)
        except (TypeError, ValueError):
            return None
        if n < 1:
            return None
        # "win by N+" is the push-free Asian Handicap at -(N-0.5) on that team.
        return {"type": "ah", "bet_id": 4,
                "side": "Home" if subject == "home" else "Away", "line": n - 0.5,
                "label": f"{subject} win by {n}+"}

    if market in _MATCH_OU:
        ou = _line_from_threshold(comp, threshold)
        if not ou:
            return None
        side, line = ou
        return {"type": "ou", "bet_id": _MATCH_OU[market], "side": side, "line": line,
                "label": f"{market} {side} {line}"}

    if market in _TEAM_OU:
        if subject not in ("home", "away"):
            return None
        ou = _line_from_threshold(comp, threshold)
        if not ou:
            return None
        side, line = ou
        bet_id = _TEAM_OU[market][0 if subject == "home" else 1]
        return {"type": "ou", "bet_id": bet_id, "side": side, "line": line,
                "label": f"{subject} {market} {side} {line}"}

    if market in _TEAM_YESNO:
        if subject not in ("home", "away"):
            return None
        # "score a goal" is yes/no; "score MORE goals than" is a comparison we
        # have no clean market for — don't price it as a plain team-to-score.
        if comp == "more":
            return None
        bet_id = _TEAM_YESNO[market][0 if subject == "home" else 1]
        spec = {"type": "select", "bet_id": bet_id, "value": "Yes",
                "label": f"{market} {subject} Yes"}
        if market == "team_score" and intent.get("excludes_own_goals"):
            spec["contract_proxy"] = TEAM_SCORE_NO_OWN_GOALS_PROXY
            spec["proxy_note"] = TEAM_SCORE_NO_OWN_GOALS_PROXY_NOTE
        return spec

    if market in _COMPARE:
        if subject not in ("home", "away"):
            return None
        spec = {"type": "select", "bet_id": _COMPARE[market],
                "value": "Home" if subject == "home" else "Away",
                "label": f"{market} {subject}"}
        if market == "cards_compare":
            spec["contract_proxy"] = CARDS_COMPARE_PROXY
            spec["proxy_note"] = CARDS_COMPARE_PROXY_NOTE
        return spec

    if market == "player_goal_scorer" and intent.get("player"):
        return {"type": "player_yes", "bet_id": 92,
                "player": intent["player"], "label": "player anytime scorer"}

    if market == "player_card" and intent.get("player"):
        return {"type": "player_yes", "bet_id": 251,
                "player": intent["player"], "label": "player booked"}

    # API-Football quotes player thresholds as single-sided "Player - N+" odds.
    if market == "player_shots_on_target":
        ou = _line_from_threshold(comp, threshold)
        if not ou:
            return None
        side, line = ou
        return {"type": "player_threshold", "bet_id": 242, "side": side, "line": line,
                "player": intent.get("player"), "label": "player SoT"}

    return None


def match_intent_oddsapi(
    intent: dict, home: str, away: str, *, stage: str | None = None,
) -> dict | None:
    """Map an intent to an Odds API market spec (fallback source).

    Returns a spec for `oddsapi.predict`, including the `market` key to fetch.
    """
    market = intent.get("market")
    subject = intent.get("subject")
    comp = intent.get("comparator")
    threshold = intent.get("threshold")
    period = intent.get("period", "match")
    player = intent.get("player")
    if not market or market == "none":
        return None
    market = _normalize(market, subject, comp, period)
    if (
        str(stage or "").lower() == "knockout"
        and intent.get("time_scope") == "full_match"
        and period == "match"
        and market != "to_advance"
    ):
        return None
    if period in ("1H", "2H"):
        return None  # Odds API half markets not wired in v0

    team = home if subject == "home" else away if subject == "away" else None

    if market == "match_winner" and team:
        return {"market": "h2h", "kind": "multiway", "name": team, "label": f"{team} win"}
    if market == "btts":
        return {"market": "btts", "kind": "yesno", "value": "Yes", "label": "BTTS"}
    if market == "corners_compare" and team:
        return {"market": "corners_1x2", "kind": "multiway", "name": team,
                "label": f"{team} more corners"}
    if market == "double_chance" and team:
        return {"market": "draw_no_bet", "kind": "multiway", "name": team,
                "label": f"{team} DNB"}

    # match/team totals → Odds API over/under markets
    _OU_MARKET = {
        "total_goals": "totals", "total_corners": "alternate_totals_corners",
        "total_cards": "alternate_totals_cards",
    }
    if market in _OU_MARKET:
        ou = _line_from_threshold(comp, threshold)
        if not ou:
            return None
        side, line = ou
        return {"market": _OU_MARKET[market], "kind": "ou", "side": side, "line": line,
                "label": f"{market} {side} {line}"}

    # player props
    if market == "player_goal_scorer" and player:
        return {"market": "player_goal_scorer_anytime", "kind": "player_yesno",
                "player": player, "label": f"{player} to score"}
    if market == "player_score_or_assist" and player:
        return {"market": "player_to_score_or_assist", "kind": "player_yesno",
                "player": player, "label": f"{player} score/assist"}
    if market == "player_card" and player:
        return {"market": "player_to_receive_card", "kind": "player_yesno",
                "player": player, "label": f"{player} booked"}
    if market == "player_shots_on_target" and player:
        ou = _line_from_threshold(comp, threshold)
        if not ou:
            return None
        side, line = ou
        return {"market": "player_shots_on_target", "kind": "player_ou", "player": player,
                "side": side, "line": line, "label": f"{player} SoT {side} {line}"}
    return None
