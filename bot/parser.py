"""LLM question parser.

Turns a free-text binary question (e.g. "Will Argentina win the match?") into a
structured intent the matcher can map to an odds market. All of a match's
questions are parsed in ONE batched call to keep token spend minimal.

Model: configurable via ``PARSER_MODEL`` (default ``gpt-5.4-mini``). See README.
"""
from __future__ import annotations

import json
import re

import requests

from . import cache, config
from .matcher import MARKET_KEYS
from .teams import normalize_team

# Bump to invalidate cached intents after a prompt/model semantics change.
PROMPT_VERSION = "p4-knockout-wording"

SYSTEM = """You convert soccer betting questions into structured JSON intents.
Each question is a YES/NO question about a single match. Output ONLY the
subject, market and parameters needed to price it. Do NOT estimate probability.

Allowed "market" values (use exactly one, or "none" if no market fits):
{keys}

Field rules:
- subject: "home" | "away" | "match" | "player"
    home/away = the team the question is ABOUT (use the given home/away names).
- player: full player name if subject is "player", else null.
- comparator:
    "win"             team to win the match (market=match_winner)
    "yes"             a yes/no event happens (btts, team_score, etc.)
    "gte"             a count is >= threshold ("N or more", "at least N")
    "lte"             a count is <= threshold ("N or fewer", "N or less")
    "more"            subject team has strictly MORE than the other team
    "second_half_more" second half has more goals than first half
- threshold: the integer N from the question for gte/lte (else null).
- period: "match" | "1H" | "2H".

If the subject is a named PERSON (not one of the two teams), it is ALWAYS a
player market with subject="player" — never a team_* market.

Player markets (subject="player", set the player's full name):
  player_goal_scorer       to score a goal (excluding own goals)
  player_score_or_assist   to score OR assist a goal
  player_card              to be booked / receive a card
  player_shots_on_target   shots on target over/under (use comparator gte/lte)

Important direct mappings:
- "at halftime, will the match be tied" -> market=match_draw, subject=match,
  comparator=yes, period=1H.
- a team to be winning at halftime, or to score more goals than its opponent in
  the second half -> market=match_winner, subject=home/away, comparator=win,
  period=1H/2H.
- a team to receive more cards than its opponent -> market=cards_compare,
  subject=home/away, comparator=more.
- match total shots on target -> market=total_shots_on_target, subject=match.
  A single team's shots-on-target total is market=team_shots_on_target.
- a team to score the first goal of the game -> market=first_team_to_score,
  subject=home/away, comparator=yes, period=match.

Knockout markets (emit when the wording fits):
- a team to qualify/advance to the next round -> to_advance.
- a team to keep a clean sheet -> team_clean_sheet; to score in both halves ->
  team_score_both_halves.
- total shots (on AND off target, NOT "shots on target") -> total_shots; a single
  team's -> team_shots.
- a team to win BY N or more goals (winning margin) -> win_margin, comparator=gte,
  threshold=N (never team_total_goals).
- both teams to receive a card -> both_teams_card; a penalty awarded ->
  penalty_awarded; a red card shown -> red_card.
A name written "Player (Country)" is ALWAYS that player, never the country's team.
"in regulation (90 minutes + stoppage time)" means the full match (period=match).

Use market="none" for compound questions (two events joined by AND/OR). Set
period to "1H"/"2H" for first/second-half or "at halftime" questions, else
"match".

Return a JSON object: {{"intents": [{{"id": <int>, "market": ..., "subject": ...,
"player": ..., "comparator": ..., "threshold": ..., "period": ...}}, ...]}}
One intent per question, preserving the given id."""


def chat_json(messages: list[dict], model: str | None = None) -> str:
    """Cached, deterministic JSON chat call (parser + compound splitter).

    The OpenAI response is keyed on (prompt version, model, exact messages) and
    cached forever (ttl=0). Identical questions therefore return byte-identical
    intents on every re-run, so a question can never flap between sources or
    probabilities across runs — and re-runs cost $0. ``temperature=0`` + a fixed
    ``seed`` make the first (cache-miss) call as reproducible as the API allows.
    """
    model = model or config.PARSER_MODEL
    key = json.dumps(
        {"v": PROMPT_VERSION, "model": model, "messages": messages},
        sort_keys=True,
    )
    return cache.get_or_fetch("llm", key, lambda: _client_chat(messages, model), ttl=0)


def _client_chat(messages: list[dict], model: str) -> str:
    r = requests.post(
        "https://api.openai.com/v1/chat/completions",
        headers={"Authorization": f"Bearer {config.OPENAI_API_KEY}"},
        json={
            "model": model,
            "messages": messages,
            "temperature": 0,
            "seed": 7,
            "response_format": {"type": "json_object"},
        },
        timeout=60,
    )
    r.raise_for_status()
    data = r.json()
    return data["choices"][0]["message"]["content"]


def parse_questions(
    questions: list[dict], home: str, away: str
) -> dict[str, dict]:
    """Parse recurring templates locally, then batch unfamiliar questions.

    questions: [{id, question}]. Returns {market_id: intent}.
    """
    out: dict[str, dict] = {}
    unfamiliar: list[tuple[dict, str]] = []  # (question, normalized text)
    for question in questions:
        cleaned = _normalize_question(question["question"], home, away)
        intent = _parse_template(cleaned, home, away)
        if intent:
            out[question["id"]] = _repair_intent(cleaned, intent, home, away)
        else:
            unfamiliar.append((question, cleaned))

    if not unfamiliar:
        return out
    if not config.OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY not set — unfamiliar question requires parser")

    numbered = [{"id": i, "question": cleaned}
                for i, (_q, cleaned) in enumerate(unfamiliar)]
    user = (
        f"Home team: {home}\nAway team: {away}\n\n"
        f"Questions:\n{json.dumps(numbered, indent=0)}"
    )
    content = chat_json(
        [
            {"role": "system", "content": SYSTEM.format(keys="\n".join(MARKET_KEYS))},
            {"role": "user", "content": user},
        ]
    )
    parsed = json.loads(content)
    for intent in parsed.get("intents", []):
        idx = intent.get("id")
        if isinstance(idx, int) and 0 <= idx < len(unfamiliar):
            question, cleaned = unfamiliar[idx]
            out[question["id"]] = _repair_intent(cleaned, intent, home, away)
    return out


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
) -> dict:
    return {
        "market": market,
        "subject": subject,
        "player": player,
        "comparator": comparator,
        "threshold": threshold,
        "period": period,
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
    text = re.sub(r"\s{2,}", " ", text).replace(" ?", "?").strip()
    return text


def _strip_team_parenthetical(text: str, team: str) -> str:
    """Drop a "(...)" whose contents normalize to ``team`` (keeps other parens)."""
    target = normalize_team(team)

    def repl(match: re.Match) -> str:
        return "" if normalize_team(match.group(1)) == target else match.group(0)

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

    # Compound decomposition happens in derive; the top-level parser only needs
    # to keep these questions out of the unfamiliar-question LLM batch.
    if (re.search(r"\b(?:AND|OR)\b", question)
            or "score the first goal of the game and" in lower):
        return _intent("none")

    # Time-window / match-state questions with no single pre-match contract: the
    # web-grounded LLM layer prices them from enriched related odds. A hydration
    # break is NOT a half (the breaks are at 22' and 67'), so these stay
    # market="none", period="match" — never a 1H/2H line.
    if ("hydration break" in lower
            or "stoppage time" in lower or "added time" in lower
            or "substitution be made before" in lower
            or lower.startswith(("will any player", "will a substitute",
                                 "will a player"))):
        return _intent("none")

    # Discipline events: emit dedicated intents so the matcher takes the exact
    # line (penalty bet 163 / red card 335/86) when a book quotes it, and degrades
    # to related odds + the deterministic estimate otherwise.
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

    if re.search(r"second half (?:have|produce|score) more goals than the "
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
        if "win by" in lower:
            count = _threshold_in(lower)
            if count and count[0] == "gte":
                return _intent("win_margin", side, "gte", count[1])
        if lower.endswith(" win the match?") or lower.endswith(" win?"):
            return _intent("match_winner", side, "win")
        if "ahead at halftime" in lower or "winning at halftime" in lower:
            return _intent("match_winner", side, "win", period="1H")
        if "advance to the round of 16" in lower or "advance to round of 16" in lower:
            return _intent("to_advance", side)
        if "keep a clean sheet" in lower:
            return _intent("team_clean_sheet", side)
        if "score in both halves" in lower:
            return _intent("team_score_both_halves", side)
        if ("score the first goal of the game" in lower
                or "score the first goal of the match" in lower):
            return _intent("first_team_to_score", side)
        if re.search(r"\bscore(?: at least 1 goal| in the (?:first|second) half)\?", lower):
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


def _repair_intent(question: str, intent: dict, home: str, away: str) -> dict:
    """Deterministically repair common parser ambiguities supported by the text."""
    intent = dict(intent)
    subject = intent.get("subject")
    if subject not in ("home", "away", "match", "player"):
        if normalize_team(subject) == normalize_team(home):
            intent["subject"] = "home"
        elif normalize_team(subject) == normalize_team(away):
            intent["subject"] = "away"

    lower = question.lower()

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

    if "score or assist" in lower:
        intent["market"] = "player_score_or_assist"
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
