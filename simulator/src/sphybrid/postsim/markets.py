"""Conservative grammar and resolvers for event/player questions outside the frozen baseline."""

from __future__ import annotations

import re
import unicodedata
import zlib
from dataclasses import dataclass, field

import numpy as np

from sportspredict.features.context import MatchContext
from sportspredict.markets.schema import MarketSpec, MarketType, apply_comparator
from sportspredict.model.outcome import MatchOutcome
from sportspredict.types import (
    CORNERS,
    GOALS,
    H1,
    H2,
    RESULT_A,
    RESULT_B,
    SHOTS_ON_TARGET,
    TEAM_A,
    TEAM_B,
)

from .timing import TimingModel
from .timeline import GoalTimeline, card_timeline, count_timeline

try:
    from sportspredict.markets.parser import (
        _LEADING_Q,
        _detect_half,
        _find_teams,
        player_name_match,
    )
    _PARSER_OK = True
except Exception:  # pragma: no cover
    _PARSER_OK = False

FIRST_GOAL = "first_goal"
FIRST_GOAL_HALF = "first_goal_half"
FIRST_CARD_BEFORE_FIRST_GOAL = "first_card_before_first_goal"
GOAL_WINDOW = "goal_window"
CARD_WINDOW = "card_window"
STAT_WINDOW = "stat_window"
SUBSTITUTION_BEFORE_HALF = "substitution_before_halftime"
SUBSTITUTE_SCORE = "substitute_score"
SUBSTITUTE_GOAL_INVOLVEMENT = "substitute_score_or_assist"
TEAM_SCORE_NO_OWN = "team_score_no_own"
ANY_PLAYER_THRESHOLD = "any_player_threshold"
COMPOUND_AND = "compound_and"
WIN_BOTH_HALVES = "win_both_halves"
EXACT_GOAL_MARGIN = "exact_goal_margin"
TEAM_CORNERS_AND_TOTAL_SHOTS_MORE = "team_corners_and_total_shots_more"
REGULATION_STANDARD = "regulation_standard"
RED_CARD = "red_card"
BOTH_TEAMS_CARD = "both_teams_card"
TOTAL_SHOTS_THRESHOLD = "total_shots_threshold"
WIN_MARGIN = "win_margin"
LEAD_ANY_TIME = "lead_any_time"
CARDS_MORE_THAN_GOALS = "cards_more_than_goals"
PLAYER_FULL_MATCH = "player_full_match"
FIRST_GOAL_ASSISTED = "first_goal_assisted"
TEAM_TWO_PLUS_SAME_HALF = "team_two_plus_same_half"
PENALTY_SCORED = "penalty_scored"
PLAYER_SOT_COMPARE = "player_sot_compare"
TEAM_UNIQUE_SHOOTERS = "team_unique_shooters"
TO_ADVANCE = "to_advance"
FIRST_HYDRATION_MINUTE = 22.0
SECOND_HYDRATION_MINUTE = 70.0

_LABEL = {"A": TEAM_A, "B": TEAM_B}
_HALF_IDX = {"1H": H1, "2H": H2}
_FIRST_RE = re.compile(r"first goal|opening goal|open the scoring|scores? first|first to score")
_SCORE_RE = re.compile(r"\bscores?\b|\bgoals?\b|\bnets?\b|scoresheet")
_PLAYER_QUALIFIER_RE = re.compile(
    r"\s+\(([^()]*)\)(?=\s+(?:score|have|record|register|get|take|attempt|fire|make|hit)\b)",
    re.IGNORECASE,
)


@dataclass
class ExtSpec:
    market: str
    params: dict = field(default_factory=dict)
    raw_question: str = ""

    @property
    def family(self) -> str:
        return self.market


def _strip_lead(raw: str) -> str:
    core = raw.lower().strip().rstrip("?").strip()
    core = re.sub(r"\s*\(90 minutes \+ stoppage time\)", "", core)
    core = re.sub(r"\s*\(excluding own goals\)", "", core)
    core = re.sub(r"\s+in regulation\b", "", core)
    return _LEADING_Q.sub("", core).strip() if _PARSER_OK else core


def _regulation_only(raw_lower: str) -> bool:
    return any(token in raw_lower for token in (
        "regulation", "90 minutes", "excluding extra time",
    ))


def _threshold(text: str) -> tuple[str, float] | None:
    for comp, pattern in (
        (">=", r"(?:at least\s+)?(\d+)\s+(?:or more|or greater)"),
        (">=", r"at least\s+(\d+)"),
        (">", r"more than\s+(\d+)"),
        ("<=", r"(\d+)\s+or (?:fewer|less)"),
    ):
        match = re.search(pattern, text)
        if match:
            return comp, float(match.group(1))
    return None


def _parse_numeric_window(clause: str) -> tuple[float, float] | None:
    if not re.search(r"\bmin(?:ute)?s?\b", clause):
        return None
    for pattern, make in (
        (r"(?:first|opening|within(?: the first)?)\s+(\d+)", lambda n: (0.0, n)),
        (r"last\s+(\d+)", lambda n: (90.0 - n, 90.0)),
        (r"before\s+(?:the\s+)?(?:minute\s+)?(\d+)", lambda n: (0.0, n)),
        (r"after\s+(?:the\s+)?(?:minute\s+)?(\d+)", lambda n: (n, 120.0)),
    ):
        match = re.search(pattern, clause)
        if match:
            return make(float(match.group(1)))
    return None


def _parse_leg(clause: str, ctx: MatchContext):
    teams = _teams_in_text(clause, ctx)
    half = _detect_half(clause)
    if _FIRST_RE.search(clause):
        if not teams:
            return None
        return (("first", _LABEL[teams[0]], half)
                if half in ("1H", "2H") else ("first", _LABEL[teams[0]]))
    win = _parse_numeric_window(clause)
    if win:
        team = _LABEL[teams[0]] if len(teams) == 1 else None
        return ("window", team, win[0], win[1])
    if _SCORE_RE.search(clause):
        if half in ("1H", "2H"):
            if len(teams) == 1:
                return ("half", _LABEL[teams[0]], _HALF_IDX[half])
            if not teams:
                return ("goal_in_half", _HALF_IDX[half])
        elif len(teams) == 1:
            return ("scores", _LABEL[teams[0]])
    return None


def _teams_in_text(clause: str, ctx: MatchContext) -> list[str]:
    """Team labels in textual order, including names that themselves contain ``and``."""
    lower = clause.lower()
    aliases = getattr(ctx, "extra", {}).get("aliases", {}) or {}
    occurrences: list[tuple[int, int, str]] = []
    for label, team in (("A", ctx.team_a), ("B", ctx.team_b)):
        names = [team, *(aliases.get(label) or [])]
        for name in names:
            if not name:
                continue
            for match in re.finditer(rf"(?<!\w){re.escape(str(name).lower())}(?!\w)", lower):
                occurrences.append((match.start(), match.end(), label))
    # Drop a shorter occurrence contained inside the other team's longer name ("Guinea" inside
    # "Equatorial Guinea"), while retaining a later standalone occurrence of the shorter name.
    valid = [
        item for item in occurrences
        if not any(
            other[2] != item[2] and other[0] <= item[0] and other[1] >= item[1]
            and (other[1] - other[0]) > (item[1] - item[0])
            for other in occurrences
        )
    ]
    found = [(min(start for start, _end, lab in valid if lab == label), label)
             for label in ("A", "B") if any(lab == label for _start, _end, lab in valid)]
    if found:
        return [label for _, label in sorted(found)]
    return _find_teams(clause, ctx)


def _qualified_baseline_spec(question: str, ctx: MatchContext):
    """Parse a baseline market after removing a SportPredict ``Player (Team)`` qualifier."""
    from sportspredict.markets import parse_question

    qualifier = _PLAYER_QUALIFIER_RE.search(question)
    clean = _PLAYER_QUALIFIER_RE.sub("", question)
    # The frozen player's leading-capital regex is ASCII-only ("Álvarez"/"Ødegaard" fail).
    clean = clean.translate(str.maketrans({"Ø": "O", "ø": "o", "Ł": "L", "ł": "l",
                                           "Đ": "D", "đ": "d", "Þ": "Th", "þ": "th"}))
    clean = unicodedata.normalize("NFKD", clean).encode("ascii", "ignore").decode()
    spec = parse_question(clean, ctx)
    if qualifier and spec.market in {
        MarketType.PLAYER_SCORE, MarketType.PLAYER_SCORE_OR_ASSIST, MarketType.PLAYER_STAT,
    }:
        teams = _teams_in_text(qualifier.group(1), ctx)
        if teams:
            spec.params["team"] = teams[0]
    return spec


def _player_sot_comparison(question: str, ctx: MatchContext) -> ExtSpec | None:
    """Parse the exact two-player strict shots-on-target comparison contract.

    Requiring both team qualifiers is deliberate. Without this rule the frozen
    parser sees the country names and silently turns a player comparison into a
    team-vs-team comparison. A less explicit form remains unsupported instead
    of guessing either player's team.
    """
    clean = re.sub(
        r"\s*\(90 minutes \+ stoppage time\)", "", question, flags=re.IGNORECASE,
    )
    clean = re.sub(r"\s+in regulation\b", "", clean, flags=re.IGNORECASE)
    match = re.fullmatch(
        r"\s*will\s+(.+?)\s+\(([^()]*)\)\s+"
        r"(?:record|register|have)\s+more\s+shots?\s+on\s+target\s+than\s+"
        r"(.+?)\s+\(([^()]*)\)\s*\?\s*",
        clean,
        re.IGNORECASE,
    )
    if not match:
        return None
    left_player, left_qualifier, right_player, right_qualifier = (
        part.strip() for part in match.groups()
    )
    left_teams = _teams_in_text(left_qualifier, ctx)
    right_teams = _teams_in_text(right_qualifier, ctx)
    if len(left_teams) != 1 or len(right_teams) != 1 or left_teams[0] == right_teams[0]:
        return None
    return ExtSpec(PLAYER_SOT_COMPARE, {
        "left_player": left_player,
        "left_team": _LABEL[left_teams[0]],
        "right_player": right_player,
        "right_team": _LABEL[right_teams[0]],
        "stat": SHOTS_ON_TARGET,
        "regulation": True,
    }, question)


def parse_extended(question: str, ctx: MatchContext) -> ExtSpec | None:
    """Claim only exact unsupported templates; every other question remains baseline-owned."""
    if not _PARSER_OK:
        return None
    core = _strip_lead(question)
    raw_lower = question.lower()

    # Official knockout progression is not a regulation 1X2 result. This
    # dedicated route covers both ordinary qualification wording and the final's
    # equivalent tournament-winner wording before the baseline can reduce the
    # latter to a 90-minute win.
    teams = _teams_in_text(core, ctx)
    eventual_winner = bool(re.search(
        r"\badvance\b|\bqualif(?:y|ies|ied|ication)\b|"
        r"\bwin (?:the )?(?:fifa )?world cup\b",
        core,
    ))
    if ctx.is_knockout and eventual_winner and len(teams) == 1:
        label = teams[0]
        team_idx = _LABEL[label]
        return ExtSpec(TO_ADVANCE, {
            "team": team_idx,
            "side": label,
            "team_name": ctx.team_a if team_idx == TEAM_A else ctx.team_b,
            "scope": "full",
        }, question)

    # The frozen parser treats the words "both teams" as a per-team
    # conjunction even when the explicit parenthetical says combined. Preserve
    # the raw settlement wording and construct the exact match-total spec.
    if _regulation_only(raw_lower) and "(both teams combined)" in raw_lower:
        count = _threshold(core)
        stat = (
            "offsides" if re.search(r"\boffsides?\b", core)
            else "corners" if re.search(r"\bcorners?(?: kicks?)?\b", core)
            else None
        )
        if count and stat:
            return ExtSpec(REGULATION_STANDARD, {
                "baseline_spec": MarketSpec(
                    MarketType.COUNT_THRESHOLD,
                    {
                        "stat": stat,
                        "scope": "match",
                        "team": None,
                        "comparator": count[0],
                        "threshold": count[1],
                        "half": "full",
                    },
                    question,
                ),
                "regulation": True,
            }, question)

    # These exact final-match specials must precede every generic baseline
    # fallback. Four of them otherwise parse as a broader, incorrect contract:
    # penalty scored -> awarded; player SOT -> team SOT; same-half goals ->
    # full-match team goals; first-goal assist -> generic first-goal rejection.
    player_compare = _player_sot_comparison(question, ctx)
    if player_compare is not None:
        return player_compare
    if (
        _regulation_only(raw_lower)
        and "first goal" in core
        and re.search(r"credited with an assist|be assisted|have an assist", core)
    ):
        return ExtSpec(FIRST_GOAL_ASSISTED, {"regulation": True}, question)
    if (
        _regulation_only(raw_lower)
        and "either team" in core
        and "same half" in core
        and re.search(r"\bscore|\bgoals?", core)
    ):
        count = _threshold(core)
        if count == (">=", 2.0):
            return ExtSpec(TEAM_TWO_PLUS_SAME_HALF, {
                "threshold": 2, "regulation": True,
            }, question)
    if (
        _regulation_only(raw_lower)
        and re.search(r"\bpenalty kick be scored\b|\bscored penalty\b", core)
    ):
        return ExtSpec(PENALTY_SCORED, {"regulation": True}, question)
    if (
        _regulation_only(raw_lower)
        and re.search(r"\b(?:different|distinct)\b", core)
        and re.search(r"\bplayers?\b", core)
        and re.search(r"\b(?:attempt|take|record|have)s?\s+(?:at least\s+)?a\s+shot\b", core)
    ):
        count = _threshold(core)
        teams = _teams_in_text(core, ctx)
        if count and count[0] == ">=" and len(teams) == 1:
            return ExtSpec(TEAM_UNIQUE_SHOOTERS, {
                "team": _LABEL[teams[0]],
                "comparator": count[0],
                "threshold": int(count[1]),
                "regulation": True,
            }, question)

    # Match operations/player aggregate props must precede generic scoring parsing.
    if "substitution be made before halftime" in core:
        return ExtSpec(SUBSTITUTION_BEFORE_HALF, {}, question)
    if re.search(r"\ba substitute score or assist", core):
        return ExtSpec(SUBSTITUTE_GOAL_INVOLVEMENT, {"regulation": True}, question)
    if re.search(r"\ba substitute score", core):
        return ExtSpec(SUBSTITUTE_SCORE, {"regulation": True}, question)
    if "first goal" in core and "second half" in core and not _teams_in_text(core, ctx):
        return ExtSpec(FIRST_GOAL_HALF, {"half": "2H", "regulation": True}, question)
    if "card" in core and "before the first goal" in core:
        return ExtSpec(FIRST_CARD_BEFORE_FIRST_GOAL, {"regulation": True}, question)
    if "win both halves" in core and "either team" in core:
        return ExtSpec(WIN_BOTH_HALVES, {"regulation": True}, question)
    margin_exact = re.search(r"\bdecided by exactly\s+(\d+|one)\s+goals?\b", core)
    if margin_exact:
        margin = 1 if margin_exact.group(1) == "one" else int(margin_exact.group(1))
        return ExtSpec(EXACT_GOAL_MARGIN, {
            "margin": margin, "regulation": True,
        }, question)
    if "card" in core and "each half" in core:
        count = _threshold(core) or (">=", 1.0)
        return ExtSpec(CARD_WINDOW, {
            "window": "each_half", "include_et": False,
            "comparator": count[0], "threshold": count[1],
        }, question)
    if (
        "card" in core
        and re.search(r"stoppage(?:\s*\(added\))?\s+time|added\s+time", core)
    ):
        return ExtSpec(CARD_WINDOW, {
            "window": "stoppage_any", "include_et": False,
            "comparator": ">=", "threshold": 1.0,
        }, question)
    if "more corner" in core and "more total shots" in core:
        teams = _teams_in_text(core, ctx)
        if teams:
            return ExtSpec(TEAM_CORNERS_AND_TOTAL_SHOTS_MORE, {
                "team": _LABEL[teams[0]], "regulation": True,
            }, question)
    lead_any_time = re.search(r"\bhold a lead at any point\b", core)
    if lead_any_time:
        teams = _teams_in_text(core, ctx)
        if teams:
            return ExtSpec(LEAD_ANY_TIME, {
                "team": _LABEL[teams[0]],
                "include_et": not _regulation_only(raw_lower),
            }, question)
    if "more total cards than total goals" in core:
        return ExtSpec(CARDS_MORE_THAN_GOALS, {
            "regulation": _regulation_only(raw_lower),
        }, question)
    full_match_player = re.search(r"(.+?)\s+play the entire match\b", core)
    if full_match_player:
        player = re.sub(r"\s+\([^)]*\)", "", full_match_player.group(1)).strip()
        if player:
            return ExtSpec(PLAYER_FULL_MATCH, {
                "player": player,
                "regulation": _regulation_only(raw_lower),
            }, question)
    if re.search(r"goal (?:be )?scored in each half|goal (?:be )?scored in every half", core):
        return ExtSpec(REGULATION_STANDARD, {
            "baseline_spec": MarketSpec(
                MarketType.HALF_CONDITIONAL,
                {"subtype": "goal_in_both_halves"},
                question,
            ),
            "regulation": True,
        }, question)
    if "excluding own goals" in raw_lower and re.search(r"\bscore a goal\b", core):
        teams = _teams_in_text(core, ctx)
        if len(teams) == 1:
            return ExtSpec(TEAM_SCORE_NO_OWN, {
                "team": _LABEL[teams[0]],
                "regulation": _regulation_only(raw_lower),
            }, question)
    if core.startswith("any player"):
        count = _threshold(core)
        if count and "shot" in core and "on target" in core:
            return ExtSpec(ANY_PLAYER_THRESHOLD, {
                "stat": "shots_on_target", "comparator": count[0], "threshold": count[1],
            }, question)
        if count and re.search(r"\bscore|\bgoal", core):
            return ExtSpec(ANY_PLAYER_THRESHOLD, {
                "stat": "goals", "comparator": count[0], "threshold": count[1],
            }, question)

    # Exact best-of-32 templates that the frozen baseline does not own, or would parse too broadly.
    if "shots (on and off target)" in core:
        count = _threshold(core)
        if count:
            return ExtSpec(TOTAL_SHOTS_THRESHOLD, {
                "comparator": count[0], "threshold": count[1], "regulation": True,
            }, question)
    margin = re.search(r"\bwin by\s+(\d+)\s+or more goals?\b", core)
    if margin:
        teams = _teams_in_text(core, ctx)
        if teams:
            return ExtSpec(WIN_MARGIN, {
                "team": _LABEL[teams[0]], "threshold": int(margin.group(1)),
                "regulation": True,
            }, question)
    if re.search(r"\ba red card be shown\b", core) and "penalt" not in core:
        return ExtSpec(RED_CARD, {"regulation": _regulation_only(raw_lower)}, question)
    if re.search(r"both teams? receive at least (?:one|1) card", core):
        return ExtSpec(BOTH_TEAMS_CARD, {"regulation": True}, question)
    if re.search(r"\ba card be shown in the first half\b", core):
        return ExtSpec(CARD_WINDOW, {"window": "first_half", "include_et": False}, question)

    # The frozen parser drops the shorter team when one country name contains the other
    # ("Equatorial Guinea" vs "Guinea"). Build this otherwise-standard baseline spec from our
    # longest-match team detector so the question cannot become unsupported.
    if re.search(r"\b(?:at halftime|half-?time)\b", core) and re.search(
        r"\b(?:winning|leading|ahead|in front)\b", core
    ):
        teams = _teams_in_text(core, ctx)
        if teams:
            return ExtSpec(REGULATION_STANDARD, {
                "baseline_spec": MarketSpec(
                    MarketType.HALF_CONDITIONAL,
                    {"subtype": "halftime_lead", "team": teams[0]},
                    question,
                ),
                "regulation": True,
            }, question)

    if "card" in core and "after the second hydration break" in core:
        return ExtSpec(CARD_WINDOW, {
            "window": "after_second_hydration", "include_et": "extra time" in core,
        }, question)
    if ("offside" in core or "ruled offside" in core) and "before the first hydration break" in core:
        return ExtSpec(STAT_WINDOW, {
            "stat": "offsides", "event_type": "offsides", "comparator": ">=", "threshold": 1.0,
            "window": "before_first_hydration",
        }, question)
    if "corner" in core and "before the first hydration break" in core:
        count = _threshold(core)
        if count:
            return ExtSpec(STAT_WINDOW, {
                "stat": "corners", "event_type": "corners", "comparator": count[0],
                "threshold": count[1], "window": "before_first_hydration",
            }, question)

    if "goal" in core and "before the first hydration break" in core:
        return ExtSpec(GOAL_WINDOW, {"window": "before_first_hydration"}, question)
    if (
        "goal" in core
        and "after the first hydration break" in core
        and ("first half" in core or "1st half" in core)
    ):
        return ExtSpec(GOAL_WINDOW, {"window": "after_first_hydration_1h"}, question)
    if "goal" in core and "after the second hydration break" in core:
        regulation_only = _regulation_only(raw_lower)
        return ExtSpec(GOAL_WINDOW, {
            "window": "after_second_hydration", "include_et": not regulation_only,
        }, question)
    if "goal" in core and re.search(
        r"stoppage(?:\s*\(added\))?\s+time|added\s+time", core
    ):
        if re.search(r"first-?\s*(?:or|/|and)\s*second-half|first or second half|either half", core):
            return ExtSpec(GOAL_WINDOW, {
                "window": "stoppage_any", "include_et": False,
            }, question)
        half = "1H" if "first-half" in core or "first half" in core else "2H"
        return ExtSpec(GOAL_WINDOW, {"window": "stoppage", "half": half}, question)

    # Known two-leg goal conjunction. Parse its semantic separator before generic ``and`` handling,
    # because several national-team names contain the word (Bosnia and Herzegovina, Trinidad and
    # Tobago) and must remain a single team token.
    compound = re.fullmatch(
        r"(.+?)\s+score the first goal(?: of the (?:game|match))?\s+and\s+(.+)", core
    )
    if compound:
        first_teams = _teams_in_text(compound.group(1), ctx)
        second_leg = _parse_leg(compound.group(2), ctx)
        if first_teams and second_leg:
            return ExtSpec(COMPOUND_AND, {
                "legs": [("first", _LABEL[first_teams[0]]), second_leg],
            }, question)

    # Regulation is a contract, not a synonym for "full match": knockout full-match baseline
    # resolvers can include extra time. Route the exact baseline spec through a regulation-only
    # resolver. This also safely handles SportPredict's parenthesized player-team qualifier.
    if _regulation_only(raw_lower):
        try:
            baseline_spec = _qualified_baseline_spec(question, ctx)
        except Exception:
            baseline_spec = None
        if baseline_spec is not None:
            return ExtSpec(REGULATION_STANDARD, {
                "baseline_spec": baseline_spec, "regulation": True,
            }, question)

    # A parenthesized player team must never turn a player prop into a team count. Keep this
    # normalization even if a future question omits the explicit regulation wording.
    if _PLAYER_QUALIFIER_RE.search(question):
        try:
            baseline_spec = _qualified_baseline_spec(question, ctx)
        except Exception:
            baseline_spec = None
        if baseline_spec is not None and baseline_spec.market in {
            MarketType.PLAYER_SCORE, MarketType.PLAYER_SCORE_OR_ASSIST, MarketType.PLAYER_STAT,
        }:
            return ExtSpec(REGULATION_STANDARD, {
                "baseline_spec": baseline_spec, "regulation": False,
            }, question)

    # The baseline's half-card resolver counts yellows only because its red total has no phase.
    # Our learned event clock can place red cards correctly, so claim every exact card count or
    # comparison and include both yellow and red cards.
    if "card" in core:
        try:
            baseline_spec = _qualified_baseline_spec(question, ctx)
        except Exception:
            baseline_spec = None
        if baseline_spec is not None and (
            baseline_spec.market == MarketType.COUNT_THRESHOLD
            and baseline_spec.params.get("stat") == "cards"
            or baseline_spec.market == MarketType.TEAM_VS_TEAM_MORE
            and baseline_spec.params.get("stat") == "cards"
        ):
            return ExtSpec(REGULATION_STANDARD, {
                "baseline_spec": baseline_spec, "regulation": _regulation_only(raw_lower),
            }, question)

    if re.search(r"\band\b", core):
        parts = re.split(r"\s+and\s+", core)
        if len(parts) == 2:
            legs = [_parse_leg(p, ctx) for p in parts]
            if all(legs) and ({leg[0] for leg in legs} & {"first", "window"} or legs[0] != legs[1]):
                return ExtSpec(COMPOUND_AND, {"legs": legs}, question)

    leg = _parse_leg(core, ctx)
    if leg and leg[0] == "first":
        regulation_only = _regulation_only(raw_lower)
        return ExtSpec(FIRST_GOAL, {
            "team": leg[1],
            "half": leg[2] if len(leg) > 2 else None,
            "include_et": len(leg) <= 2 and not regulation_only,
        }, question)
    if leg and leg[0] == "window":
        return ExtSpec(GOAL_WINDOW, {"window": "numeric", "team": leg[1],
                                     "lo": leg[2], "hi": leg[3]}, question)
    return None


def _window_mask(timeline, params: dict) -> np.ndarray:
    window = params["window"]
    if window == "before_first_hydration":
        return timeline.select(through=FIRST_HYDRATION_MINUTE, phases={"1H"})
    if window == "after_first_hydration_1h":
        return timeline.select(after=FIRST_HYDRATION_MINUTE, phases={"1H"})
    if window == "after_second_hydration":
        phases = {"2H", "ET"} if params.get("include_et") else {"2H"}
        return timeline.select(after=SECOND_HYDRATION_MINUTE, phases=phases)
    if window == "stoppage":
        return timeline.select(stoppage=params["half"])
    if window == "stoppage_any":
        return timeline.select(stoppage="1H") | timeline.select(stoppage="2H")
    if window == "first_half":
        return timeline.select(phases={"1H"})
    phases = {"1H", "2H", "ET"} if params.get("include_et") else {"1H", "2H"}
    return timeline.select(after=params.get("lo"), through=params.get("hi"), phases=phases)


def _resolve_leg(leg, timeline: GoalTimeline, outcome: MatchOutcome) -> np.ndarray:
    kind = leg[0]
    if kind == "first":
        return timeline.first_scorer_is(leg[1], leg[2] if len(leg) > 2 else None)
    if kind == "window":
        return timeline.any_goal_in_window(leg[2], leg[3], leg[1], phases={"1H", "2H"})
    if kind == "half":
        return outcome.goals_half(leg[1], leg[2]) >= 1
    if kind == "goal_in_half":
        return outcome.match_goals_half(leg[1]) >= 1
    if kind == "scores":
        return outcome.goals_team(leg[1], include_et=False) >= 1
    raise ValueError(f"unknown extended leg {leg!r}")


def _ever_leads(timeline: GoalTimeline, team: int, *, include_et: bool) -> np.ndarray:
    phases = {"1H", "2H", "ET"} if include_et else {"1H", "2H"}
    selected = np.where(timeline.select(phases=phases))[0]
    led = np.zeros(timeline.n_sims, dtype=bool)
    if selected.size == 0:
        return led
    phase_rank = np.array([_PHASE_IDX_VALUE.get(phase, 99) for phase in timeline.phase[selected]])
    order = np.lexsort((timeline.order[selected], phase_rank, timeline.world[selected]))
    score = np.zeros((2, timeline.n_sims), dtype=int)
    other = TEAM_B if team == TEAM_A else TEAM_A
    for idx in selected[order]:
        world = timeline.world[idx]
        scorer = int(timeline.team[idx])
        if scorer in (TEAM_A, TEAM_B):
            score[scorer, world] += 1
            if score[team, world] > score[other, world]:
                led[world] = True
    return led


_PHASE_IDX_VALUE = {"1H": 0, "2H": 1, "ET": 2}


def _player_full_match_probability(params: dict, ctx: MatchContext | None) -> float:
    if ctx is None:
        return 0.10
    player = str(params.get("player") or "").strip()
    best = None
    for team_idx, team_label in ((TEAM_A, "A"), (TEAM_B, "B")):
        for candidate in ctx.lineup_for(team_idx):
            score = player_name_match(player, candidate.name) if _PARSER_OK else 0
            if best is None or score > best[0]:
                best = (score, candidate, team_label)
    if best is None or best[0] <= 0:
        return 0.05
    candidate = best[1]
    start_prob = float(candidate.start_prob or 0.0)
    if start_prob < 0.5:
        return 0.02
    base_by_pos = {"GK": 0.97, "DF": 0.72, "MF": 0.54, "FW": 0.42}
    base = base_by_pos.get(str(candidate.position or "MF").upper(), 0.54)
    minutes = candidate.expected_minutes
    if minutes is not None:
        try:
            minutes = float(minutes)
        except (TypeError, ValueError):
            minutes = None
    if minutes is not None:
        if minutes >= 88:
            base = max(base, 0.82)
        elif minutes >= 84:
            base = max(base, base + 0.08)
        elif minutes < 75:
            base = min(base, 0.32)
    return float(np.clip(start_prob * base, 0.01, 0.99))


def resolve_extended(
    spec: ExtSpec, timeline: GoalTimeline, outcome: MatchOutcome, *,
    timing: TimingModel | None = None, rng: np.random.Generator | None = None,
    ctx: MatchContext | None = None, settings=None, player_shares=None,
    event_cache: dict | None = None, event_seed: int | None = None,
) -> float:
    timing = timing or TimingModel()
    rng = rng or np.random.default_rng(0)
    et_scale = 0.30
    if settings is not None:
        et_scale = (30.0 / 90.0) * float(settings.goals_model.get("et_fatigue", 0.90))

    cache = event_cache if event_cache is not None else {}

    def cached(key: str, factory):
        if key not in cache:
            cache[key] = factory()
        return cache[key]

    def stream(key: str) -> np.random.Generator:
        if event_seed is None:
            return rng
        stream_id = zlib.crc32(key.encode("utf-8")) & 0xFFFFFFFF
        return np.random.default_rng(np.random.SeedSequence([int(event_seed), stream_id]))

    def reds():
        from .timeline import red_card_timeline

        return cached(
            "red_cards",
            lambda: red_card_timeline(
                outcome, timing, stream("red_cards"), et_scale=et_scale,
            ),
        )

    def penalties():
        from .timeline import penalty_timeline

        return cached(
            "penalties",
            lambda: penalty_timeline(
                outcome, timing, stream("penalties"), et_scale=et_scale,
            ),
        )

    def cards():
        return cached(
            "cards",
            lambda: card_timeline(
                outcome, timing, stream("yellow_cards"), et_scale=et_scale, red=reds(),
            ),
        )

    def team_total_shots():
        from .shots import sample_team_total_shots

        model = (timing.data.get("models") or {}).get("total_shots")
        return cached(
            "team_total_shots",
            lambda: sample_team_total_shots(
                outcome, model, stream("team_total_shots"),
            ),
        )

    if spec.market == TO_ADVANCE:
        result_code = RESULT_A if spec.params["team"] == TEAM_A else RESULT_B
        return float(np.mean(outcome.result == result_code))
    if spec.market == FIRST_GOAL_ASSISTED:
        # The configured assisted-goal fraction is conditional on a goal. No
        # goal is explicitly NO, so averaging this conditional probability over
        # the shared regulation goal worlds exactly enforces the 0-0 rule.
        assisted_share = float(np.clip(
            settings.players.get("prob_goal_assisted", 0.70) if settings is not None else 0.70,
            0.0,
            1.0,
        ))
        any_regulation_goal = outcome.match_total(GOALS, include_et=False) >= 1
        return float(np.mean(any_regulation_goal.astype(float) * assisted_share))
    if spec.market == TEAM_TWO_PLUS_SAME_HALF:
        threshold = int(spec.params.get("threshold", 2))
        regulation = np.asarray(outcome.reg_counts[GOALS])
        mask = np.any(regulation >= threshold, axis=(0, 1))
    elif spec.market == PENALTY_SCORED:
        penalty_events = penalties()
        selected = penalty_events.select(phases={"1H", "2H"})
        attempts = penalty_events.counts(selected)
        conversion = float(np.clip(
            timing.parameter("penalty_conversion", 0.78), 0.0, 1.0,
        ))
        # Analytic binomial thinning avoids a second layer of Monte Carlo. A
        # converted kick must also fit inside a world with a regulation goal;
        # the baseline currently simulates goals and awards as correlated but
        # separate counts, so this guard preserves that logical invariant.
        probability = 1.0 - (1.0 - conversion) ** attempts
        probability *= outcome.match_total(GOALS, include_et=False) >= 1
        return float(np.clip(np.mean(probability), 0.0, 1.0))
    elif spec.market == PLAYER_SOT_COMPARE:
        if ctx is None or settings is None:
            raise ValueError("player comparison requires context and settings")
        from .allocation import prob_player_stat_more

        return prob_player_stat_more(
            outcome,
            ctx,
            stat=spec.params["stat"],
            left_player=spec.params["left_player"],
            left_team=spec.params["left_team"],
            right_player=spec.params["right_player"],
            right_team=spec.params["right_team"],
            shares=player_shares,
            settings=settings,
        )
    elif spec.market == TEAM_UNIQUE_SHOOTERS:
        if ctx is None or settings is None:
            raise ValueError("unique-shooter market requires context and settings")
        from .allocation import prob_team_unique_shooters

        team = spec.params["team"]
        return prob_team_unique_shooters(
            team_total_shots()[team],
            ctx,
            team,
            int(spec.params["threshold"]),
            player_shares,
            settings,
        )
    elif spec.market == FIRST_GOAL:
        mask = timeline.first_scorer_is(
            spec.params["team"], spec.params.get("half"),
            include_et=bool(spec.params.get("include_et")),
        )
    elif spec.market == FIRST_GOAL_HALF:
        if spec.params.get("half") != "2H":
            raise ValueError(f"unknown first-goal half {spec.params.get('half')!r}")
        mask = (outcome.match_goals_half(H1) == 0) & (outcome.match_goals_half(H2) >= 1)
    elif spec.market == FIRST_CARD_BEFORE_FIRST_GOAL:
        card_events = cards()
        first_card = np.full(outcome.n_sims, np.inf)
        first_goal = np.full(outcome.n_sims, np.inf)
        card_mask = card_events.select(phases={"1H", "2H"})
        goal_mask = timeline.select(phases={"1H", "2H"})
        if card_mask.any():
            np.minimum.at(first_card, card_events.world[card_mask], card_events.order[card_mask])
        if goal_mask.any():
            np.minimum.at(first_goal, timeline.world[goal_mask], timeline.order[goal_mask])
        # Card + no goal is YES. Neither event is NO because first_card is infinite.
        mask = np.isfinite(first_card) & (first_card < first_goal)
    elif spec.market == GOAL_WINDOW:
        mask = timeline.any(_window_mask(timeline, spec.params))
    elif spec.market == CARD_WINDOW:
        card_events = cards()
        window = spec.params.get("window")
        if window == "each_half":
            first = card_events.counts(card_events.select(phases={"1H"}))
            second = card_events.counts(card_events.select(phases={"2H"}))
            mask = (
                apply_comparator(first, spec.params["comparator"], spec.params["threshold"])
                & apply_comparator(second, spec.params["comparator"], spec.params["threshold"])
            )
        elif window == "stoppage_any":
            selected = (
                card_events.select(stoppage="1H") | card_events.select(stoppage="2H")
            )
            counts = card_events.counts(selected)
            mask = apply_comparator(counts, spec.params["comparator"], spec.params["threshold"])
        else:
            mask = card_events.any(_window_mask(card_events, spec.params))
    elif spec.market == RED_CARD:
        red_events = reds()
        phases = {"1H", "2H"} if spec.params.get("regulation") else {"1H", "2H", "ET"}
        mask = red_events.any(red_events.select(phases=phases))
    elif spec.market == BOTH_TEAMS_CARD:
        card_events = cards()
        selected = card_events.select(phases={"1H", "2H"})
        mask = np.ones(outcome.n_sims, dtype=bool)
        for team in (TEAM_A, TEAM_B):
            mask &= card_events.any(selected & (card_events.team == team))
    elif spec.market == LEAD_ANY_TIME:
        mask = _ever_leads(
            timeline, spec.params["team"], include_et=bool(spec.params.get("include_et")),
        )
    elif spec.market == CARDS_MORE_THAN_GOALS:
        include_et = not spec.params.get("regulation", False)
        card_events = cards()
        phases = {"1H", "2H", "ET"} if include_et else {"1H", "2H"}
        card_count = card_events.counts(card_events.select(phases=phases))
        goal_count = outcome.match_total(GOALS, include_et=include_et)
        mask = card_count > goal_count
    elif spec.market == STAT_WINDOW:
        events = cached(
            f"count:{spec.params['event_type']}",
            lambda: count_timeline(
                outcome, spec.params["stat"], spec.params["event_type"], timing,
                stream(f"count:{spec.params['event_type']}"),
            ),
        )
        values = events.counts(_window_mask(events, spec.params))
        mask = apply_comparator(values, spec.params["comparator"], spec.params["threshold"])
    elif spec.market == SUBSTITUTION_BEFORE_HALF:
        stage = getattr(ctx, "stage", None) if ctx is not None else None
        return timing.rate("substitution_before_halftime", stage, default=0.10)
    elif spec.market in (SUBSTITUTE_SCORE, SUBSTITUTE_GOAL_INVOLVEMENT, ANY_PLAYER_THRESHOLD):
        if ctx is None or settings is None:
            raise ValueError("player-event markets require context and settings")
        from .allocation import (
            prob_any_player_threshold,
            prob_substitute_goal_involvement,
            prob_substitute_scores,
        )
        if spec.market == SUBSTITUTE_SCORE:
            return prob_substitute_scores(
                outcome, ctx, player_shares, settings,
                fallback_share=timing.parameter("substitute_goal_share", 0.12),
                own_goal_share=timing.parameter("own_goal_share", 0.015),
            )
        if spec.market == SUBSTITUTE_GOAL_INVOLVEMENT:
            return prob_substitute_goal_involvement(
                outcome, ctx, player_shares, settings,
                fallback_goal_share=timing.parameter("substitute_goal_share", 0.12),
                fallback_assist_share=timing.parameter("substitute_assist_share", 0.14),
                own_goal_share=timing.parameter("own_goal_share", 0.015),
            )
        return prob_any_player_threshold(
            outcome, ctx, spec.params["stat"], spec.params["comparator"],
            spec.params["threshold"], player_shares, settings,
            unassigned_share=(
                timing.parameter("own_goal_share", 0.015)
                if spec.params["stat"] == "goals" else 0.0
            ),
        )
    elif spec.market == PLAYER_FULL_MATCH:
        return _player_full_match_probability(spec.params, ctx)
    elif spec.market == TEAM_SCORE_NO_OWN:
        goals = outcome.goals_team(
            spec.params["team"], include_et=not spec.params.get("regulation", False),
        )
        own_goal_share = float(timing.parameter("own_goal_share", 0.015))
        mask = 1.0 - own_goal_share ** np.asarray(goals, dtype=int)
        return float(np.clip(np.mean(mask), 0.0, 1.0))
    elif spec.market == TOTAL_SHOTS_THRESHOLD:
        values = team_total_shots()
        match_total = values[TEAM_A] + values[TEAM_B]
        mask = apply_comparator(
            match_total, spec.params["comparator"], spec.params["threshold"],
        )
    elif spec.market == WIN_MARGIN:
        team = spec.params["team"]
        other = TEAM_B if team == TEAM_A else TEAM_A
        margin = (
            outcome.goals_team(team, include_et=False)
            - outcome.goals_team(other, include_et=False)
        )
        mask = margin >= int(spec.params["threshold"])
    elif spec.market == WIN_BOTH_HALVES:
        a_h1 = outcome.goals_half(TEAM_A, H1)
        a_h2 = outcome.goals_half(TEAM_A, H2)
        b_h1 = outcome.goals_half(TEAM_B, H1)
        b_h2 = outcome.goals_half(TEAM_B, H2)
        mask = ((a_h1 > b_h1) & (a_h2 > b_h2)) | ((b_h1 > a_h1) & (b_h2 > a_h2))
    elif spec.market == EXACT_GOAL_MARGIN:
        margin = np.abs(
            outcome.goals_team(TEAM_A, include_et=False)
            - outcome.goals_team(TEAM_B, include_et=False)
        )
        mask = margin == int(spec.params["margin"])
    elif spec.market == TEAM_CORNERS_AND_TOTAL_SHOTS_MORE:
        team = spec.params["team"]
        other = TEAM_B if team == TEAM_A else TEAM_A
        corner_margin = (
            outcome.team_total(CORNERS, team, include_et=False)
            > outcome.team_total(CORNERS, other, include_et=False)
        )
        total_shots = team_total_shots()
        mask = corner_margin & (total_shots[team] > total_shots[other])
    elif spec.market == REGULATION_STANDARD:
        if ctx is None or settings is None:
            raise ValueError("standard regulation markets require context and settings")
        from sportspredict.config import Settings
        from sportspredict.markets import resolve as resolve_baseline
        from .allocation import resolve_player_goal_alloc, resolve_player_stat_alloc

        baseline = spec.params["baseline_spec"]
        market = baseline.market
        params = baseline.params
        if market in (MarketType.PLAYER_SCORE, MarketType.PLAYER_SCORE_OR_ASSIST):
            return resolve_player_goal_alloc(
                params, outcome, ctx, player_shares, settings,
                include_assist=market == MarketType.PLAYER_SCORE_OR_ASSIST,
                include_et=not spec.params.get("regulation", False),
                own_goal_share=timing.parameter("own_goal_share", 0.015),
            )
        if market == MarketType.PLAYER_STAT:
            value = resolve_player_stat_alloc(
                params, outcome, ctx, player_shares, settings,
                include_et=not spec.params.get("regulation", False),
            )
            if value is not None:
                return value
        if market in (MarketType.PENALTY_AWARDED, MarketType.PENALTY_OR_RED):
            pens = penalties()
            phases = {"1H", "2H"} if spec.params.get("regulation") else {"1H", "2H", "ET"}
            any_pen = pens.any(pens.select(phases=phases))
            if market == MarketType.PENALTY_AWARDED:
                return float(np.mean(any_pen))
            red_events = reds()
            any_red = red_events.any(red_events.select(phases=phases))
            return float(np.mean(any_pen | any_red))
        is_card_market = (
            market == MarketType.COUNT_THRESHOLD and params.get("stat") == "cards"
            or market == MarketType.TEAM_VS_TEAM_MORE and params.get("stat") == "cards"
        )
        if is_card_market:
            card_events = cards()
            half = params.get("half", "full")
            phases = ({"1H"} if half == "1H" else {"2H"} if half == "2H"
                      else {"1H", "2H"} if spec.params.get("regulation")
                      else {"1H", "2H", "ET"})
            selected = card_events.select(phases=phases)
            values = [
                card_events.counts(selected & (card_events.team == team))
                for team in (TEAM_A, TEAM_B)
            ]
            if market == MarketType.TEAM_VS_TEAM_MORE:
                subject = _LABEL[params["subject"]]
                other = TEAM_B if subject == TEAM_A else TEAM_A
                return float(np.mean(values[subject] > values[other]))
            if params["scope"] == "team":
                counts = values[_LABEL[params["team"]]]
                ok = apply_comparator(counts, params["comparator"], params["threshold"])
            elif params["scope"] == "each_team":
                ok = (
                    apply_comparator(values[0], params["comparator"], params["threshold"])
                    & apply_comparator(values[1], params["comparator"], params["threshold"])
                )
            else:
                ok = apply_comparator(
                    values[0] + values[1], params["comparator"], params["threshold"],
                )
            return float(np.mean(ok))
        rules = dict(settings.market_rules)
        rules["include_extra_time_in_counts"] = not spec.params.get("regulation", False)
        reg_settings = Settings(raw=settings.raw, market_rules=rules, root=settings.root)
        return resolve_baseline(baseline, outcome, ctx, reg_settings)
    elif spec.market == COMPOUND_AND:
        mask = _resolve_leg(spec.params["legs"][0], timeline, outcome)
        for leg in spec.params["legs"][1:]:
            mask &= _resolve_leg(leg, timeline, outcome)
    else:
        raise ValueError(f"unknown extended market {spec.market!r}")
    return float(np.clip(np.mean(mask), 0.0, 1.0))
