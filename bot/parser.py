"""LLM question parser.

Turns a free-text binary question (e.g. "Will Argentina win the match?") into a
structured intent the matcher can map to an odds market. All of a match's
questions are parsed in ONE batched call to keep token spend minimal.

Model: configurable via ``PARSER_MODEL`` (default ``gpt-4.1``). See README.
"""
from __future__ import annotations

import json
import re

import requests

from . import cache, config
from .matcher import MARKET_KEYS
from .teams import normalize_team

# Bump to invalidate cached intents after a prompt/model semantics change.
PROMPT_VERSION = "p2-deterministic"

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
    """Parse all questions for one match.

    questions: [{id, question}]. Returns {market_id: intent}.
    """
    if not config.OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY not set — parser requires it")

    numbered = [{"id": i, "question": q["question"]} for i, q in enumerate(questions)]
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
    out: dict[str, dict] = {}
    for intent in parsed.get("intents", []):
        idx = intent.get("id")
        if isinstance(idx, int) and 0 <= idx < len(questions):
            question = questions[idx]
            out[question["id"]] = _repair_intent(
                question["question"], intent, home, away
            )
    return out


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
    question_tokens = set(normalize_team(question).split())
    team_tokens = set(normalize_team(team).split())
    return bool(team_tokens) and team_tokens <= question_tokens
