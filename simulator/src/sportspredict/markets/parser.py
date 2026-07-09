"""Deterministic natural-language -> :class:`MarketSpec` parser.

This is a rules/regex pipeline, NOT a learned model: the same question always yields the
same spec, and an unrecognized question raises :class:`ParseError` rather than guessing.
Team names are resolved against the :class:`MatchContext` (team A vs B). The pipeline tries
the most specific market families first.

Covered phrasings (see ``tests/test_parser.py`` for the full matrix):
  * compound      "Will there be a penalty or a red card?"
  * player        "Will Kylian Mbappe score or assist?"
  * BTTS          "Will both teams score?"
  * result        "Will Brazil win?" / "Will the match be a draw?"
  * comparison    "Will Brazil commit more fouls than Croatia?",
                  "Will France have more second-half corners than England?"
  * half-goals    "Will there be a goal in the first half?",
                  "Will more goals be scored in the second half than the first?",
                  "Will Brazil score in both halves?"
  * total goals   "Will there be under 2.5 goals?", "Will the match have 2 or fewer goals?"
  * count thresh. "Will there be 2 or more offsides?", "At least 3 corners?"
"""

from __future__ import annotations

import re
import unicodedata

from ..features.context import MatchContext
from .schema import MarketSpec, MarketType


class ParseError(ValueError):
    """Raised when a question cannot be mapped to a known market."""


# A leading interrogative is semantically void but capitalized, so it would otherwise be
# captured as part of a player name ("Will Schick ...") and pollute team detection
# ("Can Brazil ..." finding team CAN).
_LEADING_Q = re.compile(r"^\s*(?:will|can|could|does|do|is|are)\s+", re.IGNORECASE)


# Stat synonyms -> canonical stat name. Multi-word keys are matched before single words.
_STAT_SYNONYMS: list[tuple[str, str]] = [
    ("shots on target", "shots_on_target"),
    ("shot on target", "shots_on_target"),
    ("shots-on-target", "shots_on_target"),
    ("sot", "shots_on_target"),
    ("offsides", "offsides"),
    ("offside", "offsides"),
    ("corners", "corners"),
    ("corner", "corners"),
    ("fouls", "fouls"),
    ("foul", "fouls"),
    ("bookings", "cards"),
    ("booking", "cards"),
    ("yellow cards", "cards"),
    ("yellow card", "cards"),
    ("cards", "cards"),
    ("card", "cards"),
    ("goals", "goals"),
    ("goal", "goals"),
]

_COMPARATOR_PATTERNS: list[tuple[str, str]] = [
    (r"(?:at least|minimum of|no fewer than)\s+(\d+\.?\d*)", ">="),
    (r"(\d+\.?\d*)\s+or more", ">="),
    (r"(?:at most|maximum of|no more than)\s+(\d+\.?\d*)", "<="),
    (r"(\d+\.?\d*)\s+or (?:fewer|less)", "<="),
    (r"(?:more than|over|greater than|above)\s+(\d+\.?\d*)", ">"),
    (r"(?:fewer than|less than|under|below)\s+(\d+\.?\d*)", "<"),
    (r"(?:>=|≥)\s*(\d+\.?\d*)", ">="),
    (r"(?:<=|≤)\s*(\d+\.?\d*)", "<="),
    (r">\s*(\d+\.?\d*)", ">"),
    (r"<\s*(\d+\.?\d*)", "<"),
    (r"exactly\s+(\d+\.?\d*)", "=="),
]


def _fold(s: str) -> str:
    """Accent-fold to ASCII ("Türkiye" -> "turkiye") so feed spellings match aliases."""
    return unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()


def _detect_stat(text: str) -> str | None:
    for syn, canon in _STAT_SYNONYMS:
        if re.search(rf"\b{re.escape(syn)}\b", text):
            return canon
    return None


def _detect_half(text: str) -> str:
    if re.search(r"\b(?:second[ -]half|2nd half|2h)\b", text):
        return "2H"
    # "at halftime" / "at the break" = cumulative through the first half.
    if re.search(r"\b(?:first[ -]half|1st half|1h|half-?time|at the break|\bht\b)\b", text):
        return "1H"
    return "full"


def _detect_comparator(text: str) -> tuple[str, float] | None:
    for pat, comp in _COMPARATOR_PATTERNS:
        m = re.search(pat, text)
        if m:
            return comp, float(m.group(1))
    return None


def _team_needles(ctx: MatchContext, label: str, name: str) -> set[str]:
    """Whole identifiers plus distinctive name tokens for one team (accent-folded)."""
    aliases = ctx.extra.get("aliases", {}) if ctx.extra else {}
    needles: set[str] = set()
    for n in [name, *aliases.get(label, [])]:
        if not n:
            continue
        needles.add(_fold(n))
        needles |= _name_tokens(n)
    return needles


def _token_eq(a: str, b: str) -> bool:
    """Exact word match, or a 4+ char shared prefix ("czech" ~ "czechia", "korea" ~
    "korean"). Short identifiers (3-letter codes) only match exactly."""
    if a == b:
        return True
    return len(a) >= 4 and len(b) >= 4 and (a.startswith(b[:4]) or b.startswith(a[:4]))


def _find_teams(text: str, ctx: MatchContext) -> list[str]:
    """Return ['A'] / ['B'] / ['A','B'] in order of first appearance in the text.

    Identifiers match as whole words only (code "SCO" must not fire inside "score"),
    and any distinctive token of a multi-word name counts, so a fragment like
    "Herzegovina" still resolves to Bosnia and Herzegovina. Tokens shared by both
    teams (e.g. "Korea") are ignored as ambiguous.
    """
    text = _fold(text)
    words = [(m.start(), m.group(0)) for m in re.finditer(r"[a-z][\w'’-]*", text)]
    needles_a = _team_needles(ctx, "A", ctx.team_a)
    needles_b = _team_needles(ctx, "B", ctx.team_b)
    ambiguous = needles_a & needles_b
    found: list[tuple[int, str]] = []
    for label, needles in (("A", needles_a), ("B", needles_b)):
        positions = []
        for needle in needles - ambiguous:
            if " " in needle:  # multi-word identifier: word-bounded phrase match
                m = re.search(rf"\b{re.escape(needle)}\b", text)
                if m:
                    positions.append(m.start())
            else:
                positions.extend(pos for pos, w in words if _token_eq(needle, w))
        if positions:
            found.append((min(positions), label))
    return [label for _, label in sorted(found)]


def parse_question(question: str, ctx: MatchContext) -> MarketSpec:
    raw = question.strip()
    t = " " + _LEADING_Q.sub("", raw.lower().strip()) + " "

    # 0) Extra time / shootout occurrence — most specific first ("penalty shootout"
    #    contains "penalt", so this must precede the penalty rules).
    if re.search(r"extra[ -]time", t) and re.search(
        r"\bgo(?:es|ing)? (?:in)?to\b|head to|reach|require|need|there be|be played", t
    ):
        return MarketSpec(MarketType.GOES_TO_ET, {}, raw)
    if re.search(r"shoot-?out|decided (?:by|on) penalties", t) and re.search(
        r"\bgo(?:es|ing)? to\b|head to|reach|require|need|there be|decided|end in", t
    ):
        return MarketSpec(MarketType.GOES_TO_SHOOTOUT, {}, raw)

    # 1) Compound penalty OR red card.
    if "penalt" in t and re.search(r"red card", t) and " or " in t:
        return MarketSpec(MarketType.PENALTY_OR_RED, {}, raw)

    # 1b) Standalone penalty awarded.
    if re.search(r"penalt", t) and "red card" not in t and re.search(
        r"award|penalty kick|be a penalty|penalty be|penalty given|penalty will|penalty in the", t
    ):
        return MarketSpec(MarketType.PENALTY_AWARDED, {}, raw)

    teams = _find_teams(t, ctx)
    stat = _detect_stat(t)
    half = _detect_half(t)
    comp = _detect_comparator(t)

    # 1c) First/last-goal-scorer markets are not modelled (the sim has no within-half
    #     event ordering): skip rather than misprice as anytime-score / goal-in-half.
    if re.search(r"first goal|last goal|opening goal|score first|scores first", t):
        raise ParseError(f"scored-first/last market not modelled, skipping: {question!r}")

    # 2) Both-teams markets. "Both teams score AND <N+ total goals>" is a compound
    #    (conjunction of BTTS and the match total). Plain "both teams to score"
    #    (threshold absent or trivially >=1 goal) is BTTS; with a real count it is a
    #    per-team conjunction ("both teams to have 1+ shots on target" means EACH team).
    both_each = re.search(r"both teams?|each team", t)
    either = re.search(r"either team", t)
    if (
        re.search(r"both teams?.*\bscore\b.*\band\b.*goals", t)
        and comp is not None
    ):
        return MarketSpec(
            MarketType.BTTS_AND_TOTAL,
            {"comparator": comp[0], "threshold": comp[1], "half": half},
            raw,
        )
    if re.search(r"both teams?.*\bscore\b|\bbtts\b", t) and (
        comp is None or (comp[0] == ">=" and comp[1] <= 1.0)
    ):
        yes = not re.search(r"\bnot\b|fail", t)
        return MarketSpec(MarketType.BTTS, {"yes": yes, "half": half}, raw)
    if (both_each or either) and stat and comp is not None:
        return MarketSpec(
            MarketType.COUNT_THRESHOLD,
            {"stat": stat, "scope": "each_team" if both_each else "either_team",
             "team": None, "comparator": comp[0], "threshold": comp[1], "half": half},
            raw,
        )

    # 3) Half-time result (tied / a team leading at the break) — only the score, not a stat.
    if re.search(r"half-?time|at the break|\bht\b", t) and stat is None:
        if re.search(r"\btied\b|\blevel\b|\bdraw|\beven\b|all square", t):
            return MarketSpec(MarketType.HALF_CONDITIONAL, {"subtype": "halftime_tied"}, raw)
        if teams and re.search(r"lead|ahead|winning|in front", t):
            return MarketSpec(
                MarketType.HALF_CONDITIONAL, {"subtype": "halftime_lead", "team": teams[0]}, raw
            )

    # 3b) Odd/even total goals (before half-conditional: "an odd number of goals be
    #     scored in the first half" must not become goal_in_half).
    if stat == "goals":
        m_par = re.search(r"\b(odd|even)\b(?:\s+(?:number|total|amount))", t) or re.search(
            r"goals(?:\s+\w+){0,3}\s+be\s+(odd|even)\b", t
        )
        if m_par:
            return MarketSpec(
                MarketType.TOTAL_GOALS_PARITY, {"parity": m_par.group(1), "half": half}, raw
            )

    # 4) Half-conditional goal markets (team/both, by half) — before player markets so that
    #    "<team> score in both halves" is not mistaken for an individual. A captured player
    #    name diverts to the player rules instead ("Mbappe to score in the first half" must
    #    never resolve as the match-level goal_in_half).
    if (stat == "goals" or "score" in t) and _capture_player_name(raw, ctx) is None:
        spec = _try_half_conditional(t, ctx, teams)
        if spec is not None:
            spec.raw_question = raw
            return spec

    # 5) Player markets — a named individual to score (optionally + assist).
    pspec = _try_player(raw, t, ctx)
    if pspec is not None:
        return pspec

    # 5b) Player statistical prop (e.g. "<player> to have 1+ shots on target"). Must not fall
    #     through to a match/team count threshold (that gave 99% on a player SoT market).
    if stat and stat != "goals" and comp is not None:
        pname = _player_stat_name(raw, ctx)
        if pname:
            if stat in _PLAYER_STAT_MODELLED:
                return MarketSpec(
                    MarketType.PLAYER_STAT,
                    {"player": pname, "team": _player_team(pname, ctx), "stat": stat,
                     "comparator": comp[0], "threshold": comp[1], "half": half},
                    raw,
                )
            raise ParseError(f"player stat prop not modelled, skipping: {question!r}")

    # 5c) Team-vs-team "more than" comparison (a stat + both teams + comparative wording).
    #     Goals comparisons qualify only without a numeric threshold ("more than 2.5 goals"
    #     is a totals market, "more goals than Croatia" is a comparison).
    if (
        stat
        and len(teams) == 2
        and re.search(r"\bmore\b|\bthan\b", t)
        and (stat != "goals" or (comp is None and re.search(r"\bmore\b.*\bthan\b", t)))
    ):
        subject = teams[0]
        return MarketSpec(
            MarketType.TEAM_VS_TEAM_MORE,
            {"stat": stat, "subject": subject, "half": half},
            raw,
        )

    # 5d) Win to nil / clean sheet — before the plain result rules ("win to nil" contains
    #     "win" and must not resolve as a plain win).
    if teams and re.search(r"\bto nil\b|without conceding|clean sheet", t):
        if re.search(r"\bwin\b|\bwins\b|\bbeat\b", t):
            return MarketSpec(MarketType.WIN_TO_NIL, {"team": teams[0]}, raw)
        return MarketSpec(MarketType.CLEAN_SHEET, {"team": teams[0]}, raw)

    # 6) Match result (1X2). SportsPredict result markets are phrased "win in regulation"
    # (90 minutes), so we resolve at full-time UNLESS the question is about progression
    # ("advance"/"qualify"), which is the official extra-time/shootout outcome.
    advances = bool(re.search(r"progress|advance|qualif|reach the|go through", t))
    # 6a) Double chance ("win or draw") — before the bare draw rule.
    if teams and re.search(r"win or draw|draw or win|avoid (?:defeat|losing)|not lose", t):
        return MarketSpec(
            MarketType.MATCH_RESULT,
            {"side": teams[0], "regulation": True, "double_chance": True},
            raw,
        )
    if re.search(r"\bdraw\b|\btie\b|\bdrawn\b|\blevel\b", t) and stat != "goals":
        return MarketSpec(MarketType.MATCH_RESULT, {"side": "draw", "regulation": True}, raw)
    if re.search(r"\bwin\b|\bbeat\b|\bwins\b|progress|advance|qualif", t) and len(teams) >= 1:
        # "win in extra time" / "win the shootout" are scoped results we do not model;
        # skipping is safer than pricing them as a plain win.
        if re.search(r"extra[ -]time|overtime|shoot-?out", t) and not advances:
            raise ParseError(f"scoped result market not modelled, skipping: {question!r}")
        return MarketSpec(
            MarketType.MATCH_RESULT,
            {"side": teams[0], "regulation": not advances},
            raw,
        )

    # 7) Goal totals with a threshold: a single named team makes it a TEAM goal count
    #    ("Wales to score 2+ goals"), otherwise it is the match total.
    if stat == "goals" and comp is not None:
        if len(teams) == 1:
            return MarketSpec(
                MarketType.COUNT_THRESHOLD,
                {"stat": "goals", "scope": "team", "team": teams[0],
                 "comparator": comp[0], "threshold": comp[1], "half": half},
                raw,
            )
        return MarketSpec(
            MarketType.TOTAL_GOALS,
            {"comparator": comp[0], "threshold": comp[1], "half": half},
            raw,
        )

    # 8) Generic count threshold for any stat.
    if stat and comp is not None:
        scope = "team" if len(teams) == 1 else "match"
        return MarketSpec(
            MarketType.COUNT_THRESHOLD,
            {
                "stat": stat,
                "scope": scope,
                "team": teams[0] if scope == "team" else None,
                "comparator": comp[0],
                "threshold": comp[1],
                "half": half,
            },
            raw,
        )

    raise ParseError(f"could not parse question: {question!r}")


def _try_half_conditional(t: str, ctx: MatchContext, teams: list[str]) -> MarketSpec | None:
    # "more (total) goals in the second half than (the) first"
    if re.search(
        r"more (?:total )?goals.*second.*than.*first"
        r"|second half.*more (?:total )?goals.*than.*first", t
    ):
        return MarketSpec(MarketType.HALF_CONDITIONAL, {"subtype": "more_goals_2h"})
    # "<team> score(s) in both halves" / "a goal in both halves" (match level)
    if re.search(r"both halves", t):
        if teams:
            return MarketSpec(
                MarketType.HALF_CONDITIONAL,
                {"subtype": "team_scores_both_halves", "team": teams[0]},
            )
        return MarketSpec(MarketType.HALF_CONDITIONAL, {"subtype": "goal_in_both_halves"})
    # "goal in the first/second half" (a goal scored at all in that half)
    m = re.search(r"\b(?:first[ -]half|second[ -]half|1st half|2nd half|1h|2h)\b", t)
    if m and re.search(r"\bgoal\b|\bscore", t):
        # "A score more goals than B in the second half" is a comparison, not
        # goal-in-half — leave it for the team-vs-team rule.
        if len(teams) == 2 and re.search(r"\bmore\b.*\bthan\b", t):
            return None
        half = "1H" if re.search(r"first|1st|1h", m.group(0)) else "2H"
        if teams and re.search(r"\bscore", t):
            return MarketSpec(
                MarketType.HALF_CONDITIONAL,
                {"subtype": "team_goal_in_half", "team": teams[0], "half": half},
            )
        return MarketSpec(
            MarketType.HALF_CONDITIONAL, {"subtype": "goal_in_half", "half": half}
        )
    return None


_PLAYER_STOPWORDS = {"both", "the", "there", "a", "an", "no", "any", "either", "neither"}

# Player stat props we have a (crude) model for; others are skipped rather than mis-resolved.
_PLAYER_STAT_MODELLED = {"shots_on_target"}


def _player_stat_name(raw: str, ctx: MatchContext) -> str | None:
    """Capture a player's name in a statistical prop ("<Name> to have/record/take ...")."""
    m = re.search(
        r"([A-Z][\w.'’-]*(?:\s+[A-Z][\w.'’-]*){0,3})\s+(?:to\s+)?"
        r"(?:have|record|register|get|take|attempt|fire|make|complete|provide|hit)",
        _LEADING_Q.sub("", raw),  # a leading "Will" is capitalized and would be captured
    )
    if not m:
        return None
    name = m.group(1).strip()
    if name.lower() in _PLAYER_STOPWORDS or _looks_like_team(name, ctx):
        return None
    return name


_TEAM_TOKEN_STOP = {"and", "of", "the", "republic", "united", "states", "north", "south",
                    "dr", "pr", "new", "saudi", "rep", "island", "islands"}


def _name_tokens(s: str) -> set[str]:
    toks = set(re.sub(r"[^a-z ]", " ", _fold(s)).split())
    return {w for w in (toks - _TEAM_TOKEN_STOP) if len(w) >= 3} or toks


def _looks_like_team(name: str, ctx: MatchContext) -> bool:
    """True if the captured name refers to (or is a fragment of) one of the two teams.

    An exact token shared with a team identifier is decisive ("Herzegovina"). Fuzzy
    prefix matches only count when EVERY distinctive token of the candidate matches a
    team token: "Czechia" ~ "Czech" is the team, but "Scott McTominay" is a person even
    though "Scott" prefix-matches "Scotland".
    """
    cand = _name_tokens(name)
    if not cand:
        return False
    team_tokens = _team_needles(ctx, "A", ctx.team_a) | _team_needles(ctx, "B", ctx.team_b)
    if cand & team_tokens:
        return True
    return all(any(_token_eq(c, t) for t in team_tokens) for c in cand)


def _capture_player_name(raw: str, ctx: MatchContext) -> str | None:
    """Capture a capitalized name immediately preceding a scoring verb, if it is not a team."""
    m = re.search(
        r"([A-Z][\w.'’-]*(?:\s+[A-Z][\w.'’-]*){0,3})\s+(?:to\s+)?(?:scores?|assists?|nets?|get on the score)",
        _LEADING_Q.sub("", raw),  # a leading "Will" is capitalized and would be captured
    )
    if not m:
        return None
    name = m.group(1).strip()
    if name.lower() in _PLAYER_STOPWORDS or _looks_like_team(name, ctx):
        return None
    return name


def _try_player(raw: str, t: str, ctx: MatchContext) -> MarketSpec | None:
    """Detect a named-individual market: player to score, or score-or-assist."""
    if not re.search(r"\bscore\b|\bassist\b|\bgoal\b|scoresheet|\bnet\b", t):
        return None
    name = _capture_player_name(raw, ctx)
    if name is None:
        return None
    if re.search(r"both halves", t):
        # "player to score in both halves" is not modelled; fall through (-> ParseError)
        # rather than price it as a plain "player to score".
        return None
    assist = "assist" in t
    market = MarketType.PLAYER_SCORE_OR_ASSIST if assist else MarketType.PLAYER_SCORE
    return MarketSpec(
        market,
        {"player": name, "team": _player_team(name, ctx), "half": _detect_half(t)},
        raw,
    )


def _extract_player(raw: str) -> str:
    """Best-effort player-name extraction from the question text."""
    m = re.search(r"will\s+(.+?)\s+(?:to\s+)?(?:score|get|record|assist)", raw, re.I)
    if m:
        return re.sub(r"\b(the|a|an)\b", "", m.group(1), flags=re.I).strip()
    # Fallback: strip leading "Will" and trailing clause.
    cleaned = re.sub(r"^\s*will\s+", "", raw, flags=re.I)
    return re.split(r"\s+(?:score|assist|get|record)", cleaned, flags=re.I)[0].strip(" ?")


def _player_tokens(s: str) -> set[str]:
    """Accent-folded lowercase name tokens: 'K. Mbappé' -> {'mbappe'}.

    Tokens under 3 chars are dropped: initials, and particles like the "Al" in
    Arabic names — which once tied "Al-Taamari" to every other "Al-X" squad member,
    including a goalkeeper.
    """
    return {w for w in re.sub(r"[^a-z ]", " ", _fold(s)).split() if len(w) >= 3}


def player_name_match(query: str, candidate: str) -> int:
    """Token-overlap score between a question's player string and a lineup name.

    Token-based and accent-insensitive so a partial mention ("Schick") still finds
    "Patrik Schick" — the old bidirectional-substring test matched neither direction.
    0 means no overlap.
    """
    return len(_player_tokens(query) & _player_tokens(candidate))


def _player_team(player: str, ctx: MatchContext) -> str | None:
    """Resolve a player to team A/B via the confirmed lineup, else the squad list.

    When BOTH squads are known and the player appears in neither, the player is not at
    the tournament (or not selected) — skip the market rather than price a phantom.
    """
    best_label, best_score = None, 0
    for label, lineup in (("A", ctx.lineup_a), ("B", ctx.lineup_b)):
        for p in lineup:
            score = player_name_match(player, p.name)
            if score > best_score:
                best_label, best_score = label, score
    if best_label:
        return best_label
    squads = ctx.extra.get("squads", {}) if ctx.extra else {}
    for label in ("A", "B"):
        for p in squads.get(label, []):
            score = player_name_match(player, p.name)
            if score > best_score:
                best_label, best_score = label, score
    if best_label:
        return best_label
    if squads.get("A") and squads.get("B"):
        raise ParseError(f"player {player!r} not found in either squad, skipping")
    return None
