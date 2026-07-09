"""Deterministic SportPredict question parser.

Known competition wording is mapped locally. Unfamiliar wording is returned as
structured unresolved work and may be resolved by the manual Codex workflow;
this module never performs a network or model call.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable, Mapping

from .intent_resolution import (
    PARSER_SCHEMA_VERSION,
    lookup_resolution,
    validate_intent,
)
from .teams import normalize_team


class ParseResult(dict):
    """Resolved intents plus explicit metadata for unresolved questions.

    This remains a ``dict`` so existing pricing and evidence callers can keep
    using ``.get()``, iteration, and direct market-ID indexing.
    """

    def __init__(self) -> None:
        super().__init__()
        self.unresolved: list[dict] = []
        self.compounds: dict[str, dict] = {}
        self.intent_sources: dict[str, str] = {}
        self.resolution_provenance: dict[str, dict] = {}

    @property
    def intents(self) -> "ParseResult":
        return self


_COMPOUND_RE = re.compile(
    r"\b(?:AND|OR)\b|\bscore the first goal of the game and\b"
)


def is_compound_question(question: str) -> bool:
    """Return whether wording uses one of the supported compound forms."""
    return bool(_COMPOUND_RE.search(question))


def split_compound_template(question: str) -> dict | None:
    """Split a recurring two-leg compound without any model fallback."""
    explicit = re.fullmatch(
        r"Will (.+?)\s+(AND|OR)\s+(.+?)\?", question.strip(),
    )
    if explicit:
        first, op, second = explicit.groups()
        return {
            "op": op,
            "a": f"Will {first}?",
            "b": f"Will {second}?",
        }
    first_goal = re.fullmatch(
        r"Will (.+?) score the first goal of the game and "
        r"(.+?) score in the (first|second) half\?",
        question.strip(), re.IGNORECASE,
    )
    if first_goal:
        first, second, period = first_goal.groups()
        return {
            "op": "AND",
            "a": f"Will {first} score the first goal of the game?",
            "b": f"Will {second} score in the {period} half?",
        }
    return None


def _parse_compound(split: dict | None, home: str, away: str) -> dict | None:
    if not split or split.get("op") not in {"AND", "OR"}:
        return None
    components = []
    for key in ("a", "b"):
        question = split.get(key)
        if not isinstance(question, str) or is_compound_question(question):
            return None
        cleaned = _normalize_question(question, home, away)
        intent = _parse_template(cleaned, home, away)
        if not intent:
            return None
        repaired = validate_intent(_repair_intent(
            cleaned, intent, home, away, raw_question=question,
        ))
        components.append({"question": question, "intent": repaired})
    return {"op": split["op"], "components": components}


def parse_questions(
    questions: Iterable[Mapping],
    home: str,
    away: str,
    *,
    registry_dir: str | Path | None = None,
) -> ParseResult:
    """Parse known questions and report every unfamiliar one without raising."""
    out = ParseResult()
    seen_ids: set[str] = set()
    for index, question in enumerate(questions):
        if not isinstance(question, Mapping):
            raise ValueError(f"questions[{index}] must be an object")
        if "id" not in question or "question" not in question:
            raise ValueError(f"questions[{index}] requires id and question")
        market_id = str(question["id"])
        raw_question = question["question"]
        if not market_id or market_id in seen_ids:
            raise ValueError(f"duplicate or empty question id: {market_id!r}")
        if not isinstance(raw_question, str) or not raw_question.strip():
            raise ValueError(f"question {market_id!r} must be a non-empty string")
        seen_ids.add(market_id)
        cleaned = _normalize_question(raw_question, home, away)

        split = split_compound_template(cleaned)
        compound = _parse_compound(split, home, away) if split else None
        intent = _parse_template(cleaned, home, away)
        if intent and intent.get("market") != "none":
            # A dedicated exact contract (for example corners AND total shots)
            # is stronger than generic logical decomposition.
            compound = None
        elif split and compound:
            intent = _intent("none")
        elif is_compound_question(cleaned):
            # Recognizing a conjunction is not enough. Every component must
            # have a canonical tracked intent or Codex must resolve it.
            intent = None

        if intent:
            repaired = validate_intent(_repair_intent(
                cleaned, intent, home, away, raw_question=raw_question,
            ))
            out[market_id] = repaired
            out.intent_sources[market_id] = "tracked-rule"
            if compound:
                out.compounds[market_id] = compound
            continue

        registered = lookup_resolution(
            raw_question, home, away, registry_dir=registry_dir,
        )
        if registered:
            repaired = validate_intent(_repair_intent(
                cleaned, registered["intent"], home, away,
                raw_question=raw_question,
            ))
            out[market_id] = repaired
            out.intent_sources[market_id] = "runtime-resolution"
            if registered.get("compound"):
                out.compounds[market_id] = registered["compound"]
            out.resolution_provenance[market_id] = {
                "resolution_key": registered["resolution_key"],
                "registry_path": registered["registry_path"],
                "provenance": registered["provenance"],
            }
            continue

        out.unresolved.append({
            "market_id": market_id,
            "question": raw_question.strip(),
            "normalized_question": cleaned,
            "reason": "unrecognized-question",
        })
    return out


def parse_question_template(question: str, home: str, away: str) -> dict | None:
    """Parse one question using tracked local templates only."""
    cleaned = _normalize_question(question, home, away)
    intent = _parse_template(cleaned, home, away)
    if intent and intent.get("market") != "none":
        return validate_intent(
            _repair_intent(cleaned, intent, home, away, raw_question=question),
        )
    split = split_compound_template(cleaned)
    if split and _parse_compound(split, home, away):
        return validate_intent(_repair_intent(
            cleaned, _intent("none"), home, away, raw_question=question,
        ))
    if is_compound_question(cleaned):
        return None
    if not intent:
        return None
    return validate_intent(
        _repair_intent(cleaned, intent, home, away, raw_question=question),
    )


_COUNT_MARKETS = {
    "goal": ("total_goals", "team_total_goals"),
    "corner": ("total_corners", "team_corners"),
    "card": ("total_cards", "team_cards"),
    "offside": ("total_offsides", "team_offsides"),
    "foul": ("total_fouls", "team_fouls"),
    "shot on target": ("total_shots_on_target", "team_shots_on_target"),
}
_COMPARE_MARKETS = {
    "goal": "match_winner",
    "corner": "corners_compare",
    "card": "cards_compare",
    "offside": "offsides_compare",
    "foul": "fouls_compare",
    "shot on target": "shots_on_target_compare",
}


def _intent(
    market: str,
    subject: str = "match",
    comparator: str = "yes",
    threshold: int | None = None,
    period: str = "match",
    player: str | None = None,
    time_scope: str | None = None,
) -> dict:
    return {
        "market": market,
        "subject": subject,
        "player": player,
        "comparator": comparator,
        "threshold": threshold,
        "period": period,
        "time_scope": time_scope,
    }


def _normalize_question(question: str, home: str, away: str) -> str:
    """Strip knockout-stage boilerplate so stable templates keep matching.

    Removes the regulation/extra-time scope qualifiers ("in regulation (90
    minutes + stoppage time)", ", excluding extra time"), the "(excluding own
    goals)" gloss, and a "(Country)" parenthetical that merely names one of the
    two teams (e.g. "Jamal Musiala (Germany)" -> "Jamal Musiala"). The last one
    is what stops player props being misread as the team's own market.
    """
    text = question
    text = re.sub(r"\s*\(90 minutes \+ stoppage time\)", "", text, flags=re.I)
    text = re.sub(r",?\s*excluding extra time", "", text, flags=re.I)
    text = re.sub(r"\s+in regulation\b", "", text, flags=re.I)
    text = re.sub(r"\s*\(added\)", "", text, flags=re.I)
    text = re.sub(r"\s*\(excluding own goals\)", "", text, flags=re.I)
    for team in (home, away):
        text = _strip_team_parenthetical(text, team)
    text = re.sub(r"\s*,\s*\)", ")", text)
    text = re.sub(r"\s{2,}", " ", text).replace(" ?", "?").strip()
    leading_period = re.fullmatch(
        r"in the (first|second) half,\s*(will .+?)\?",
        text, re.IGNORECASE,
    )
    if leading_period:
        period, body = leading_period.groups()
        text = f"{body} in the {period.lower()} half?"
    return text


def _strip_team_parenthetical(text: str, team: str) -> str:
    """Drop a "(...)" whose contents normalize to ``team`` (keeps other parens)."""
    target = normalize_team(team)

    def repl(match: re.Match) -> str:
        contents = match.group(1)
        without_number = re.sub(r"(?:,\s*)?#?\d+\b", "", contents)
        without_number = re.sub(
            r"\b(?:no|number)\.?\s*\d+\b", "", without_number, flags=re.I,
        )
        return "" if normalize_team(without_number) == target else match.group(0)

    return re.sub(r"\s*\(([^()]*)\)", repl, text)


def _parse_template(question: str, home: str, away: str) -> dict | None:
    """Recognize stable Probability Cup question families conservatively.

    ``question`` is the normalized text (see ``_normalize_question``).
    """
    lower = question.strip().lower()
    if not lower.startswith(("will ", "at halftime,")):
        return None
    period = _period_from_text(lower)
    subject = _mentioned_team(question, home, away)
    before_more = question.lower().split("more", 1)[0]
    compare_subject = subject or _mentioned_team(before_more, home, away)

    # New exact specials whose wording contains AND/OR or otherwise looks like a
    # nearby standard market. Claim them before the generic compound guard.
    if (
        "more corner" in lower
        and "more total shots" in lower
        and re.search(r"\band\b", lower)
        and compare_subject
    ):
        return _intent(
            "team_corners_and_total_shots_compare", compare_subject[0], "more",
            period=period,
        )
    if "first goal" in lower and "second half" in lower and not subject:
        return _intent("first_goal_half", comparator="yes", period="2H")
    if "win both halves" in lower and "either team" in lower:
        return _intent("win_both_halves")
    if "decided by exactly" in lower and "goal" in lower:
        count = _threshold_in(lower)
        threshold = count[1] if count and count[0] == "eq" else 1 if "exactly one" in lower else None
        if threshold is not None:
            return _intent("exact_goal_margin", comparator="eq", threshold=threshold)
    if "card" in lower and "each half" in lower:
        count = _threshold_in(lower) or ("gte", 1)
        return _intent("card_each_half", comparator=count[0], threshold=count[1])
    if (
        "card" in lower
        and ("stoppage time" in lower or "added time" in lower)
        and ("first-" in lower or "first half" in lower or "second-" in lower
             or "second half" in lower)
    ):
        return _intent("card_stoppage", comparator="gte", threshold=1)

    # Compound decomposition metadata is attached by ``parse_questions``. Keep
    # the top-level market unsupported because there is no single contract.
    if (re.search(r"\b(?:AND|OR)\b", question)
            or "score the first goal of the game and" in lower):
        return _intent("none")
    if "first goal" in lower and "other than" in lower:
        return _intent("none")

    # Time-window / match-state questions. A hydration break is NOT a half (the
    # boundaries are 22' and 70'), so these stay period="match" — never a 1H/2H
    # line. Several have no provider contract but are still familiar simulator or
    # online-special markets.
    if ("penalty shootout" in lower or "shootout" in lower) and (
        "excluding a penalty shootout" not in lower
    ):
        return _intent("penalty_shootout")
    if "hydration break" in lower and "goal" in lower:
        return _intent("goal_window")
    if "hydration break" in lower:
        return _intent("none")
    if "hold a lead at any point" in lower:
        return _intent("lead_any_time", subject[0] if subject else "match")
    if "more total cards than total goals" in lower:
        return _intent("cards_more_than_goals")
    if "go to extra time" in lower or "goes to extra time" in lower:
        return _intent("goes_to_extra_time")
    if (
        "both halves" in lower
        and "same number of goals" in lower
    ) or (
        "same number of goals" in lower
        and "each half" in lower
    ):
        return _intent("highest_scoring_half_draw")
    if "first card" in lower and "before the first goal" in lower:
        return _intent("first_card_before_first_goal")
    if "total substitutions" in lower:
        count = _threshold_in(lower)
        if count:
            return _intent("total_substitutions", comparator=count[0],
                           threshold=count[1], period=period)
    exact_goals = (
        re.search(r"\bexactly\s+(\d+)\s+(?:total\s+)?goals?\b", lower)
        or re.search(r"\bexactly\s+(\d+)\s+goals?\s+be scored\b", lower)
    )
    if exact_goals:
        return _intent("total_goals", comparator="eq",
                       threshold=int(exact_goals.group(1)))
    goals_parity = (
        re.search(r"\bgoals?(?:\s+\w+){0,5}\s+be\s+(?:an?\s+)?(odd|even)\b", lower)
        or re.search(r"\b(odd|even)\s+(?:number|total|amount)\b.*\bgoals?\b", lower)
    )
    if goals_parity and "goal" in lower:
        return _intent(
            "total_goals_parity", subject="match",
            comparator=goals_parity.group(1), period=period,
        )
    if "goal be scored in each half" in lower or "goal be scored in every half" in lower:
        return _intent("goal_in_each_half")
    if "play the entire match" in lower:
        player_match = re.fullmatch(
            r"will (.+?) play the entire match\?", question.strip(), re.IGNORECASE,
        )
        return _intent(
            "player_full_match", "player",
            player=player_match.group(1) if player_match else None,
        )
    if lower.startswith("will a substitute") and "score or assist" in lower:
        return _intent("substitute_score_or_assist")
    if lower.startswith("will a substitute") and ("score" in lower or "goal" in lower):
        return _intent("substitute_score")
    if ("stoppage time" in lower or "added time" in lower
            or "substitution be made before" in lower
            or lower.startswith(("will any player", "will a player"))):
        return _intent("none")

    # Goal-method templates. Own goal has an exact API-Football Yes/No contract;
    # header/outside-the-box do not (the available provider ladders are player
    # props or first-goal method, both different contracts), so leave those to
    # simulator/web evidence without an unfamiliar parser call.
    if "own goal be scored" in lower:
        return _intent("own_goal", period=period)
    if ("header goal be scored" in lower
            or "goal be scored from outside the penalty area" in lower):
        return _intent("none", period=period)

    # Discipline events: emit dedicated intents so the matcher takes the exact
    # line (penalty bet 163 / red card 335/86) when a book quotes it, and degrades
    # to the simulator fallback otherwise.
    if "penalty kick be awarded" in lower:
        return _intent("penalty_awarded", period=period)
    if "red card" in lower and "shown" in lower:
        return _intent("red_card", period=period)
    if "both teams" in lower and "shot on target" in lower:
        return _intent("none", period=period)

    # Draw / tie contracts (full match or, when "halftime" is named, the 1st half).
    if lower.startswith("at halftime,") and re.search(
        r"\b(?:tied?|a draw|level|all square)\b", lower
    ):
        return _intent("match_draw", period="1H")
    if re.search(r"\b(?:end in a tie|be tied|be a draw|"
                 r"end (?:level|all square))\b", lower):
        return _intent("match_draw", period=period)

    if re.search(r"second half (?:have|produce|score) more (?:total )?goals than the "
                 r"first half", lower):
        return _intent("highest_scoring_half_2h", comparator="second_half_more")
    if "both teams score" in lower:
        return _intent("btts", period=period)
    if ("both teams" in lower and "card" in lower
            and ("receive" in lower or "shown" in lower)):
        return _intent("both_teams_card", period=period)

    # Total/team shots (on AND off target) — distinct from shots on target.
    if "shots" in lower and "on target" not in lower and re.search(
        r"\btotal shots\b|on and off target", lower
    ):
        count = _threshold_in(lower)
        if count:
            if subject:
                return _intent("team_shots", subject[0], count[0], count[1])
            return _intent("total_shots", comparator=count[0], threshold=count[1])

    if subject:
        side, team = subject
        if (
            "any" in lower and "player" in lower
            and "shot" in lower and "on target" in lower
        ):
            count = _threshold_in(lower)
            if count:
                return _intent(
                    "any_team_player_shots_on_target", side,
                    count[0], count[1], period,
                )
        if "win by" in lower:
            count = _threshold_in(lower)
            if count and count[0] == "gte":
                return _intent("win_margin", side, "gte", count[1])
        if lower.endswith(" win the match?") or lower.endswith(" win?"):
            return _intent("match_winner", side, "win")
        if (
            "ahead at halftime" in lower
            or "winning at halftime" in lower
            or (period == "1H" and re.search(r"\bbe winning\?", lower))
        ):
            return _intent("match_winner", side, "win", period="1H")
        if re.search(
            r"\b(?:advance|qualify|progress|go through|reach)\b.*\b(?:"
            r"round of 16|quarter-?finals?|semi-?finals?|final|next round)\b",
            lower,
        ):
            return _intent("to_advance", side)
        if "keep a clean sheet" in lower:
            return _intent("team_clean_sheet", side)
        if "score in both halves" in lower:
            return _intent("team_score_both_halves", side)
        if ("score the first goal of the game" in lower
                or "score the first goal of the match" in lower
                or "score the first goal of the second half" in lower):
            return _intent("first_team_to_score", side, period=period)
        if re.search(
            r"\bscore(?: a goal| at least 1 goal| in the (?:first|second) half)\?",
            lower,
        ):
            market = {"1H": "team_score_1h", "2H": "team_score_2h"}.get(
                period, "team_score"
            )
            return _intent(market, side, period=period)

        metric = _metric_in(lower)
        count = _threshold_in(lower)
        if metric and count:
            return _intent(
                _COUNT_MARKETS[metric][1], side, count[0], count[1], period
            )

    if compare_subject and "score more goals than" in lower:
        return _intent("match_winner", compare_subject[0], "win", period=period)
    metric = _metric_in(lower)
    if compare_subject and metric and re.search(r"\bmore\b", lower):
        return _intent(
            _COMPARE_MARKETS[metric], compare_subject[0], "more", period=period
        )

    # "a card be shown in the (first half|match)" -> at least one card.
    if re.search(r"\bcard be shown\b", lower) and "red card" not in lower:
        return _intent("total_cards", comparator="gte", threshold=1, period=period)

    count = _threshold_in(lower)
    if metric and count and lower.startswith(("will the ", "will there ")):
        return _intent(_COUNT_MARKETS[metric][0], comparator=count[0],
                       threshold=count[1], period=period)

    # Player templates are distinguished from match totals by their leading
    # proper-name phrase and by the absence of either team name.
    if not subject:
        patterns = (
            ("player_score_or_assist", r"will (.+?) score or assist a goal\?"),
            ("player_goal_scorer", r"will (.+?) score (?:a )?goal\b.*\?"),
            ("player_card", r"will (.+?) (?:be booked|receive (?:a|at least 1) card)\?"),
        )
        for market, pattern in patterns:
            player_match = re.fullmatch(pattern, question.strip(), re.IGNORECASE)
            if player_match:
                return _intent(market, "player", player=player_match.group(1))
        player_match = re.fullmatch(
            r"will (.+?) (?:have|record) (.+?shots? on target.*?)\?",
            question.strip(), re.IGNORECASE,
        )
        if player_match:
            count = _threshold_in(player_match.group(2).lower())
            if count:
                return _intent(
                    "player_shots_on_target", "player", count[0], count[1],
                    period, player_match.group(1),
                )
        player_match = re.fullmatch(
            r"will (.+?) (?:make|record|have) (.+?saves?.*?)\?",
            question.strip(), re.IGNORECASE,
        )
        if player_match:
            count = _threshold_in(player_match.group(2).lower())
            if count:
                return _intent(
                    "player_goalkeeper_saves", "player", count[0], count[1],
                    period, player_match.group(1),
                )
    return None


def _period_from_text(lower: str) -> str:
    has_1h = "first half" in lower or "1st half" in lower or "halftime" in lower
    has_2h = "second half" in lower or "2nd half" in lower
    if has_1h and has_2h:
        return "match"
    if has_2h:
        return "2H"
    return "1H" if has_1h else "match"


def _mentioned_team(question: str, home: str, away: str) -> tuple[str, str] | None:
    mentioned = [
        (side, team) for side, team in (("home", home), ("away", away))
        if _team_is_mentioned(question, team)
    ]
    return mentioned[0] if len(mentioned) == 1 else None


def _metric_in(lower: str) -> str | None:
    if re.search(r"\bshots? on target\b", lower):
        return "shot on target"
    return next((metric for metric in _COUNT_MARKETS if metric in lower), None)


def _threshold_in(lower: str) -> tuple[str, int] | None:
    patterns = (
        ("gte", r"(?:at least\s+)?(\d+)\s+(?:or more|or greater)"),
        ("gte", r"at least\s+(\d+)"),
        ("lte", r"(?:at most\s+)?(\d+)\s+or (?:fewer|less)"),
        ("lte", r"at most\s+(\d+)"),
        ("eq", r"exactly\s+(\d+)"),
    )
    for comparator, pattern in patterns:
        match = re.search(pattern, lower)
        if match:
            return comparator, int(match.group(1))
    return None


_TEAM_COUNT_MARKETS = {
    "total_goals", "total_corners", "total_cards", "total_offsides",
    "total_fouls", "total_shots_on_target",
}


def _repair_intent(
    question: str,
    intent: dict,
    home: str,
    away: str,
    *,
    raw_question: str | None = None,
) -> dict:
    """Deterministically repair common parser ambiguities supported by the text."""
    intent = dict(intent)
    subject = intent.get("subject")
    if subject not in ("home", "away", "match", "player"):
        if normalize_team(subject) == normalize_team(home):
            intent["subject"] = "home"
        elif normalize_team(subject) == normalize_team(away):
            intent["subject"] = "away"

    lower = question.lower()
    raw_lower = (raw_question or question).lower()

    # --- deterministic period detection ---
    # Push the period decision out of the LLM for the unambiguous phrasings so
    # half questions can never silently price as full-match lines (or fall to
    # the web layer) across runs.
    has_1h = bool(re.search(r"\bfirst half\b|\b1st half\b", lower))
    has_2h = bool(re.search(r"\bsecond half\b|\b2nd half\b", lower))
    has_ht = bool(re.search(r"\bhalftime\b|\bhalf[-\s]?time\b", lower))
    if has_1h and has_2h:
        # A comparison spanning both halves is the highest-scoring-half contract,
        # a full-match market — not a single-period line.
        intent["period"] = "match"
    elif has_2h:
        intent["period"] = "2H"
    elif has_1h or has_ht:
        intent["period"] = "1H"

    # Settlement scope is part of the contract, not removable boilerplate.
    # Half/window questions cannot reach extra time; otherwise omission of an
    # explicit regulation qualifier means the full match, including ET if played.
    explicit_regulation = any(token in raw_lower for token in (
        "in regulation", "during regulation", "90 minutes", "excluding extra time",
    ))
    fixed_regulation_window = bool(
        intent.get("period") in ("1H", "2H")
        or re.search(r"before (?:the first hydration break|halftime)", raw_lower)
        or "stoppage time" in raw_lower
        or "stoppage (added) time" in raw_lower
    )
    shootout_occurrence = (
        "penalty shootout" in raw_lower
        and "excluding a penalty shootout" not in raw_lower
        and re.search(
            r"decided by|go(?:es|ing)? to|head to|reach|require|there be|end in",
            raw_lower,
        )
    )
    if shootout_occurrence:
        intent["time_scope"] = "penalty_shootout"
    else:
        intent["time_scope"] = (
            "regulation" if explicit_regulation or fixed_regulation_window
            else "full_match"
        )
    intent["excludes_own_goals"] = "excluding own goals" in raw_lower

    # "At halftime, will the match be tied/level/a draw" -> 1st-half draw.
    if has_ht and re.search(r"\btied?\b|\bdraw\b|\blevel\b|\ball square\b", lower):
        intent.update(market="match_draw", subject="match",
                      comparator="yes", period="1H")

    # "<team> to score more goals than <team> [in the half]" is an outscore /
    # match-winner contract (full match -> bet 1, a half -> half winner), never
    # a totals line. The both-halves case above is excluded.
    if "more goals than" in lower and not (has_1h and has_2h):
        before = lower.split("more goals than", 1)[0]
        intent["market"] = "match_winner"
        intent["comparator"] = "win"
        for side, team in (("home", home), ("away", away)):
            if _team_is_mentioned(before, team):
                intent["subject"] = side
                break

    if "score or assist" in lower and "substitute" not in lower:
        intent["market"] = "player_score_or_assist"
    if "go to extra time" in lower or "goes to extra time" in lower:
        intent.update(market="goes_to_extra_time", subject="match",
                      comparator="yes", period="match")
    if "both halves" in lower and "same number of goals" in lower:
        intent.update(market="highest_scoring_half_draw", subject="match",
                      comparator="yes", period="match")
    if "first card" in lower and "before the first goal" in lower:
        intent.update(market="first_card_before_first_goal", subject="match",
                      comparator="yes", period="match")
    if "total substitutions" in lower:
        count = _threshold_in(lower)
        intent.update(market="total_substitutions", subject="match",
                      comparator=count[0] if count else intent.get("comparator", "yes"),
                      threshold=count[1] if count else intent.get("threshold"),
                      period="match")
    if (
        "any" in lower and "player" in lower
        and "shot" in lower and "on target" in lower
    ):
        count = _threshold_in(lower)
        mentioned = [
            side for side, team in (("home", home), ("away", away))
            if _team_is_mentioned(question, team)
        ]
        if count and len(mentioned) == 1:
            intent.update(market="any_team_player_shots_on_target",
                          subject=mentioned[0], comparator=count[0],
                          threshold=count[1], period="match")
    if "save" in lower and intent.get("subject") == "player":
        count = _threshold_in(lower)
        if count:
            intent.update(market="player_goalkeeper_saves",
                          comparator=count[0], threshold=count[1],
                          period="match")
    if "caught offside" in lower:
        intent["market"] = "team_offsides"
        if "or more" in lower or "at least" in lower:
            intent["comparator"] = "gte"
        elif "or fewer" in lower or "or less" in lower:
            intent["comparator"] = "lte"
        if "first half" not in lower and "second half" not in lower:
            intent["period"] = "match"
        mentioned = [
            side for side, team in (("home", home), ("away", away))
            if _team_is_mentioned(question, team)
        ]
        if len(mentioned) == 1:
            intent["subject"] = mentioned[0]
    if (intent.get("market") == "highest_scoring_half_2h"
            and intent.get("comparator") in ("gte", "lte")):
        intent["market"] = "total_goals"

    if intent.get("subject") != "match" or intent.get("market") not in _TEAM_COUNT_MARKETS:
        return intent
    mentioned = [
        side for side, team in (("home", home), ("away", away))
        if _team_is_mentioned(question, team)
    ]
    if len(mentioned) == 1:
        intent["subject"] = mentioned[0]
    return intent


def _team_is_mentioned(question: str, team: str) -> bool:
    normalized_question = normalize_team(question)
    question_words = normalized_question.split()
    question_tokens = set(question_words)
    team_tokens = set(normalize_team(team).split())
    if bool(team_tokens) and team_tokens <= question_tokens:
        return True
    # Alias normalization (USA -> United States, DR Congo -> Congo DR) applies
    # to whole names. Check short spans so aliases embedded in prose work too.
    target = normalize_team(team)
    return any(
        normalize_team(" ".join(question_words[start:start + size])) == target
        for size in range(1, 5)
        for start in range(len(question_words) - size + 1)
    )
