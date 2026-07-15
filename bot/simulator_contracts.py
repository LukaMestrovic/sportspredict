"""Canonical questions for exhaustive simulator contract evaluation.

These templates are not user-facing markets. They provide one stable way to
ask the frozen simulator every labelable exact contract on every settled match.
The order of returned questions matches ``wc2026_evidence.labels_for_contract``:
home then away for team-relative contracts, otherwise one match observation.
"""
from __future__ import annotations

import re


def questions_for_contract(key: str, home: str, away: str) -> list[str]:
    """Return exhaustive benchmark questions for one exact contract."""
    questions = _count_questions(key, home, away)
    if questions is not None:
        return questions
    questions = _compare_questions(key, home, away)
    if questions is not None:
        return questions
    questions = _total_goal_questions(key)
    if questions is not None:
        return questions

    fixed = {
        "goal_window:after_second_hydration:et": [
            "Will a goal be scored after the second hydration break, including extra time?"
        ],
        "goal_window:after_second_hydration:reg": [
            "Will a goal be scored after the second hydration break in regulation?"
        ],
        "goal_window:before_first_hydration:reg": [
            "Will a goal be scored before the first hydration break in regulation?"
        ],
        "goal_window:after_first_hydration_1h:reg": [
            "Will a goal be scored in the first half after the first hydration break?"
        ],
        "goal_window:stoppage:1H": [
            "Will a goal be scored in first-half stoppage time?"
        ],
        "goal_window:stoppage:2H": [
            "Will a goal be scored in second-half stoppage time?"
        ],
        "goal_window:stoppage:any:reg": [
            "Will a goal be scored during first- or second-half stoppage time in regulation?"
        ],
        "first_card_before_first_goal:reg": [
            "Will a card be shown before the first goal in regulation?"
        ],
        "card_window:cards:after_second_hydration:et:>=:1": [
            "Will a card be shown after the second hydration break, including extra time?"
        ],
        "card_window:cards:first_half:reg:>=:1": [
            "Will a card be shown in the first half in regulation?"
        ],
        "card_window:cards:each_half:reg:>=:1": [
            "Will at least one card be shown in each half in regulation?"
        ],
        "card_window:cards:stoppage_any:reg:>=:1": [
            "Will a card be shown in first- or second-half stoppage time?"
        ],
        "red_card:match": ["Will a red card be shown in the match?"],
        "both_teams_card:reg": [
            "Will both teams receive at least one card in regulation?"
        ],
        "penalty_awarded:reg": [
            "Will a penalty kick be awarded in regulation?"
        ],
        "penalty_or_red:match": [
            "Will there be a penalty kick or a red card in the match?"
        ],
        "penalty_or_red:reg": [
            "Will there be a penalty kick or a red card in regulation?"
        ],
        "substitution_before_halftime:reg": [
            "Will a substitution be made before halftime?"
        ],
        "btts:full:reg": [
            "Will both teams score in regulation?"
        ],
        "btts_and_total:reg": [
            "Will both teams score and will there be at least 3 total goals in regulation?"
        ],
        "half_conditional:halftime_tied": [
            "Will the match be tied at halftime?"
        ],
        "half_conditional:more_goals_2h": [
            "Will more goals be scored in the second half than the first half?"
        ],
        "first_goal_half:2H:reg": [
            "Will the first goal of the match be scored in the second half?"
        ],
        "win_both_halves:reg": [
            "Will either team win both halves in regulation?"
        ],
        "exact_goal_margin:reg:1": [
            "Will the match be decided by exactly 1 goal in regulation?"
        ],
        "compound:team_more_corners_and_total_shots:reg": [
            f"Will {home} have more corner kicks AND more total shots than {away} in regulation?",
            f"Will {away} have more corner kicks AND more total shots than {home} in regulation?",
        ],
        "match_result:draw:reg": [
            "Will the match be a draw in regulation?"
        ],
        "any_player_threshold:goals:>:1:reg": [
            "Will any player score more than 1 goal in regulation?"
        ],
        "any_player_threshold:shots_on_target:>=:2:reg": [
            "Will any player have at least 2 shots on target in regulation?"
        ],
        "total_shots_threshold:shots_total:>=:20:reg": [
            "Will there be at least 20 shots (on and off target) in regulation?"
        ],
        "total_shots_threshold:shots_total:>=:22:reg": [
            "Will there be at least 22 shots (on and off target) in regulation?"
        ],
        "substitute_score:reg": [
            "Will a substitute score in regulation?"
        ],
        "substitute_score_or_assist:reg": [
            "Will a substitute score or assist a goal in regulation?"
        ],
        "stat_window:corners:before_first_hydration:reg:>=:2": [
            "Will there be at least 2 corners before the first hydration break?"
        ],
        "stat_window:offsides:before_first_hydration:reg:>=:1": [
            "Will there be at least 1 offside before the first hydration break?"
        ],
    }
    if key in fixed:
        return fixed[key]
    if key == "match_result:team:reg":
        return [
            f"Will {home} win in regulation?",
            f"Will {away} win in regulation?",
        ]
    if key == "match_result:team:advance":
        return [
            f"Will {home} advance?",
            f"Will {away} advance?",
        ]
    if key == "half_conditional:halftime_lead":
        return [
            f"Will {home} be leading at halftime?",
            f"Will {away} be leading at halftime?",
        ]
    if key == "first_goal:full:team":
        return [
            f"Will {home} score the first goal in regulation?",
            f"Will {away} score the first goal in regulation?",
        ]
    if key == "first_goal:2H:team":
        return [
            f"Will {home} score the first goal in the second half?",
            f"Will {away} score the first goal in the second half?",
        ]
    if key == "compound:first_goal_and_other_team_scores_2h":
        return [
            f"Will {home} score the first goal and {away} score in the second half?",
            f"Will {away} score the first goal and {home} score in the second half?",
        ]
    if key == "win_margin:reg:2":
        return [
            f"Will {home} win by 2 or more goals in regulation?",
            f"Will {away} win by 2 or more goals in regulation?",
        ]
    if key.startswith("clean_sheet:"):
        return [
            f"Will {home} keep a clean sheet in regulation?",
            f"Will {away} keep a clean sheet in regulation?",
        ]
    return []


def observation_unit(key: str) -> str:
    """Describe the natural number of observations contributed by one fixture."""
    if re.match(r"^count:[^:]+:team:", key):
        return "team"
    if key.startswith(("compare:", "first_goal:")):
        return "team"
    if key in {
        "compound:first_goal_and_other_team_scores_2h",
        "half_conditional:halftime_lead",
        "match_result:team:reg",
        "match_result:team:advance",
        "win_margin:reg:2",
    } or key.startswith("clean_sheet:"):
        return "team"
    return "match"


def _count_questions(key: str, home: str, away: str) -> list[str] | None:
    match = re.fullmatch(
        r"count:([^:]+):(team|match|each_team):(1H|2H|full):"
        r"(>=|>|<=|<):(\d+(?:\.\d+)?):(reg|match)",
        key,
    )
    if not match:
        return None
    stat, scope, half, comparator, threshold, time_scope = match.groups()
    amount = _amount(comparator, threshold)
    period = {"1H": "in the first half", "2H": "in the second half", "full": ""}[half]
    regulation = "in regulation" if time_scope == "reg" and half == "full" else ""
    suffix = " ".join(piece for piece in (period, regulation) if piece)
    phrase = _stat_phrase(stat)
    if scope == "team":
        return [
            _question(f"Will {home} have {amount} {phrase} {suffix}?"),
            _question(f"Will {away} have {amount} {phrase} {suffix}?"),
        ]
    if scope == "each_team":
        return [_question(f"Will both teams have {amount} {phrase} {suffix}?")]
    return [_question(f"Will the match have {amount} {phrase} {suffix}?")]


def _compare_questions(key: str, home: str, away: str) -> list[str] | None:
    match = re.fullmatch(r"compare:([^:]+):(1H|2H|full):(reg|match)", key)
    if not match:
        return None
    stat, half, time_scope = match.groups()
    period = {"1H": "in the first half", "2H": "in the second half", "full": ""}[half]
    regulation = "in regulation" if time_scope == "reg" and half == "full" else ""
    suffix = " ".join(piece for piece in (period, regulation) if piece)
    phrase = _stat_phrase(stat)
    return [
        _question(f"Will {home} have more {phrase} than {away} {suffix}?"),
        _question(f"Will {away} have more {phrase} than {home} {suffix}?"),
    ]


def _total_goal_questions(key: str) -> list[str] | None:
    match = re.fullmatch(
        r"total_goals:(1H|2H|full):(>=|>|<=|<):(\d+(?:\.\d+)?):(reg|match)",
        key,
    )
    if not match:
        return None
    half, comparator, threshold, time_scope = match.groups()
    period = {"1H": "in the first half", "2H": "in the second half", "full": ""}[half]
    regulation = "in regulation" if time_scope == "reg" and half == "full" else ""
    suffix = " ".join(piece for piece in (period, regulation) if piece)
    return [_question(f"Will there be {_amount(comparator, threshold)} goals {suffix}?")]


def _amount(comparator: str, threshold: str) -> str:
    return {
        ">=": f"at least {threshold}",
        ">": f"more than {threshold}",
        "<=": f"at most {threshold}",
        "<": f"fewer than {threshold}",
    }[comparator]


def _stat_phrase(stat: str) -> str:
    return {
        "shots_on_target": "shots on target",
        "shots_total": "shots",
        "corners": "corners",
        "fouls": "fouls",
        "offsides": "offsides",
        "cards": "cards",
        "goals": "goals",
    }.get(stat, stat.replace("_", " "))


def _question(value: str) -> str:
    return re.sub(r"\s+", " ", value).replace(" ?", "?").strip()
