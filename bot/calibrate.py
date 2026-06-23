"""Layer 5 — post-anchor LLM calibration (the edge layer).

After the deterministic cascade prices every question into an *anchor*
probability, this layer fires ONE web-grounded LLM call per match ~30 minutes
before kickoff. The model sees every anchored prediction AND the exact method
behind it (source, book count, the per-book de-vigged probabilities and their
spread) plus the confirmed starting XI, researches the match on the web, and
returns small signed *tilts*.

The LLM only supplies judgement; this module owns the arithmetic and guardrails:

  * tilts are applied in **logit space** (the repo's calibration convention);
  * the realised move is hard-capped by a **per-book-count cap** — large for a
    lone or model-derived anchor that is least worth trusting (e.g. a scratched
    striker still quoted ~51% by one stale pre-match book), tiny for a deep
    multi-book consensus we should not fight;
  * soft tilts on liquid markets are further shrunk by the book spread;
  * every term (raw tilt, weight, realised delta, rationale) is recorded on each
    Prediction for the ledger / post-match review.

Determinism, cost, leakage:

  * one call per match, cached forever on ``(version, model, match_id)`` so a
    re-run is free and returns the **frozen pre-match** research — a later re-run
    cannot leak post-match information;
  * ``calibrate()`` refuses to run once kickoff has passed (leak guard);
  * off by default — enable with ``CALIBRATE_ENABLED=1``. See README "Calibration".
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
from statistics import pstdev

import requests

from . import cache, config

# Bump when the briefing schema / output contract changes. Prompt *text* edits are
# picked up automatically via the prompt hash in the cache key (see _cache_key).
CALIB_PROMPT_VERSION = "c2-sources-tiers"
MODEL = os.environ.get("CALIBRATE_MODEL", "gpt-5.5")
# The designed instruction template lives outside the code so it can be iterated
# without a code change; editing it re-keys the per-match cache.
PROMPT_PATH = config.ROOT / "prompts" / "calibration_prompt.md"

# Approx public USD per 1M tokens (input, output) for cost logging only; web
# search is billed separately per call. Update if OpenAI repricing matters.
_PRICES = {
    "gpt-5.5": (5.0, 30.0), "gpt-5": (1.25, 10.0), "gpt-5-mini": (0.25, 2.0),
    "gpt-4.1": (2.0, 8.0), "gpt-4.1-mini": (0.4, 1.6),
}
_WEB_SEARCH_CALL_USD = 0.01  # $10 / 1000 calls
# Set on every real (cache-miss) call so the caller/cron log can report spend.
LAST_USAGE: dict | None = None
# Off by default: the layer is web-grounded and non-deterministic on first call
# (a documented exception, like the external layer). Set CALIBRATE_ENABLED=1.
ENABLED = os.environ.get("CALIBRATE_ENABLED", "0") != "0"

# --- tilt-mapper constants (probability points unless noted) ---
# logit units per LLM tilt-point. 0.04 ≈ d(logit)/dp at p=0.5, so near mid-range a
# tilt of N points moves the probability ~N points (for w=1) — i.e. tilt_points are
# honest probability points, as the prompt tells the model. The per-book cap and the
# spread weight w only ever shrink that, never amplify it.
ALPHA = 0.04
SPREAD_REF = 0.06     # book-prob stdev mapping to full soft-tilt weight
W_MIN = 0.15          # floor on the soft-tilt weight for a tight liquid market
W_EMPIRICAL = 0.5     # weight for model-derived anchors (n_books == 0, no spread)
MAX_TILT = 50         # clamp on the LLM's stated tilt_points
TIMEOUT = 300         # seconds; generous — we fire at T-30 with ~1800s of headroom


def cap_for_books(n_books: int) -> int:
    """Max realised probability-point move, by anchor reliability.

    A lone book (or a model-derived anchor) is the least trustworthy, so it may
    be corrected hard toward a floor; a deep multi-book consensus is efficient
    and barely moves. Big moves only where the anchor is weak — the safe
    direction.
    """
    if n_books >= 8:
        return 6
    if n_books >= 5:
        return 8
    if n_books >= 2:
        return 18
    if n_books == 1:
        return 45
    return 18  # n_books == 0: empirical / derived model estimate


def _logit(p: float) -> float:
    return math.log(p / (1.0 - p))


def _sigmoid(z: float) -> float:
    return 1.0 / (1.0 + math.exp(-z))


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def _spread(book_probs: list[float]) -> float | None:
    """Population stdev of the per-book de-vigged probabilities (None if <2)."""
    return pstdev(book_probs) if book_probs and len(book_probs) >= 2 else None


# Concise inline fallback used only if prompts/calibration_prompt.md is missing
# (e.g. a packaging slip). The on-disk template is the source of truth.
_FALLBACK_PROMPT = """You are a sharp, well-calibrated soccer trading assistant for a \
probability competition: for each binary question you decide whether to nudge \
our probability.

You are given, for ONE match: the confirmed starting XI + bench (when available) \
and a list of questions. EACH question already has an ANCHOR probability we \
derived from bookmaker odds, plus the exact method behind it: the source, the \
number of books (n_books), the per-book de-vigged probabilities, and their \
spread. Do NOT re-price from scratch — the anchor is usually right. Apply small, \
calibrated TILTS only where late/soft information or an unreliable anchor \
justifies it.

Rules of thumb:
- Liquid markets (many books, tight spread) are efficient: lean at most a few \
points, and only with a concrete reason.
- A lone book (n_books<=1) or a model-derived anchor (n_books==0) is unreliable. \
If it is clearly wrong, a LARGE tilt is appropriate. In particular, for a \
player-prop question (to score / assist / be booked / shots) where the named \
player is NOT in the starting XI given above, tilt strongly negative (he can \
still come on as a substitute, so do not imply zero).
- A wide spread means the books disagree → more room to tilt.
- Weigh confirmed lineup/rotation, injuries from your web research, motivation \
(dead rubber vs must-win), weather/pitch, and any sharp line moves.
- Do NOT invent a player being out unless the provided XI or your sources \
confirm it. When the XI is absent, keep tilts small.

Research the specific match with web search (team news, previews, prediction \
markets, weather) before answering.

Reply with ONLY a JSON object, no prose:
{"briefing": "<=4 sentence summary of findings and how they inform the tilts",
 "sources": ["url", ...],
 "tilts": [{"market_id": "<id>", "tilt_points": <int from -50 to 50>, "rationale": "<one line>"}]}
Include a tilt entry ONLY for questions you want to move; omit the rest (== 0). \
tilt_points is in probability points (negative lowers our YES probability)."""

_prompt_cache: str | None = None


def _load_prompt() -> str:
    """The designed instruction template (cached per process; inline fallback)."""
    global _prompt_cache
    if _prompt_cache is None:
        try:
            _prompt_cache = PROMPT_PATH.read_text(encoding="utf-8")
        except OSError:
            _prompt_cache = _FALLBACK_PROMPT
    return _prompt_cache


def _cache_key(match_id: str) -> str:
    """One call per match; the prompt hash re-keys the cache when the template edits."""
    prompt_sha = hashlib.sha1(_load_prompt().encode("utf-8")).hexdigest()[:8]
    return json.dumps(
        {"v": CALIB_PROMPT_VERSION, "model": MODEL,
         "match_id": match_id, "prompt_sha": prompt_sha},
        sort_keys=True,
    )


def _tier_for_books(n_books: int) -> str:
    """Market-efficiency label shown to the model (sizing follows cap_for_books)."""
    if n_books >= 5:
        return "deep-liquid"
    if n_books >= 1:
        return "thin"
    return "no-market"


def _summarize_lineups(lineups: list[dict] | None) -> dict | None:
    """Compact {team: {formation, starting_xi, bench}} from the AF payload."""
    if not lineups:
        return None
    out: dict[str, dict] = {}
    for entry in lineups:
        team = (entry.get("team") or {}).get("name") or "?"
        xi = [(pl.get("player") or {}).get("name") for pl in entry.get("startXI", [])]
        bench = [(pl.get("player") or {}).get("name")
                 for pl in entry.get("substitutes", [])]
        out[team] = {
            "formation": entry.get("formation"),
            "starting_xi": [n for n in xi if n],
            "bench": [n for n in bench if n],
        }
    return out


def build_briefing(result, lineups, minutes_before: float) -> dict:
    """Assemble what the LLM sees: every anchor + its exact method + the XI."""
    questions = []
    for p in result.predictions:
        book_probs = list(p.book_probabilities or [])
        spread = _spread(book_probs)
        n_books = p.n_books or 0
        questions.append({
            "market_id": p.market_id,
            "question": p.question,
            "source": p.source,
            "n_books": p.n_books,
            "tier": _tier_for_books(n_books),
            "max_move": cap_for_books(n_books),
            "anchor_pct": p.probability_int,
            "book_probabilities": [round(x, 4) for x in book_probs],
            "spread": round(spread, 4) if spread is not None else None,
            "label": p.market_label,
        })
    fixture = (result.fixture or {}).get("fixture", {}) if result.fixture else {}
    venue = fixture.get("venue") or {}
    venue_str = None
    if venue.get("name"):
        venue_str = venue["name"] + (f", {venue['city']}" if venue.get("city") else "")
    return {
        "match_id": result.sp_match["id"],
        "home": result.home,
        "away": result.away,
        "kickoff": result.sp_match["opening_time"],
        "minutes_to_kickoff": round(minutes_before, 1),
        "venue": venue_str,
        "referee": fixture.get("referee"),
        "lineups": _summarize_lineups(lineups),
        "questions": questions,
    }


def _extract_json(text: str) -> dict:
    text = text.strip()
    try:
        return json.loads(text)
    except ValueError:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if not match:
            raise
        return json.loads(match.group(0))


def _record_usage(data: dict) -> None:
    """Stash + log token usage and web-search calls so we can track real spend."""
    global LAST_USAGE
    usage = data.get("usage") or {}
    in_tok = usage.get("input_tokens") or usage.get("prompt_tokens") or 0
    out_tok = usage.get("output_tokens") or usage.get("completion_tokens") or 0
    web_calls = sum(1 for o in data.get("output", [])
                    if str(o.get("type", "")).startswith("web_search"))
    in_rate, out_rate = _PRICES.get(MODEL, (0.0, 0.0))
    cost = in_tok / 1e6 * in_rate + out_tok / 1e6 * out_rate
    cost += web_calls * _WEB_SEARCH_CALL_USD
    LAST_USAGE = {"model": MODEL, "input_tokens": in_tok, "output_tokens": out_tok,
                  "web_search_calls": web_calls, "est_cost_usd": round(cost, 4)}
    print(f"[calibrate] usage model={MODEL} in={in_tok} out={out_tok} "
          f"web_calls={web_calls} est_cost=${cost:.4f}", flush=True)


def _call_llm(briefing: dict) -> dict:
    payload = {
        "model": MODEL,
        "tools": [{"type": "web_search"}],
        "reasoning": {"effort": "medium"},
        "input": f"{_load_prompt()}\n\nMATCH DATA:\n"
                 f"{json.dumps(briefing, ensure_ascii=False)}",
    }
    last_exc: Exception | None = None
    for _ in range(2):  # one retry; safe at T-30
        try:
            r = requests.post(
                "https://api.openai.com/v1/responses",
                headers={"Authorization": f"Bearer {config.OPENAI_API_KEY}"},
                json=payload, timeout=TIMEOUT,
            )
            r.raise_for_status()
            data = r.json()
            _record_usage(data)
            text = "".join(
                c.get("text", "")
                for o in data.get("output", []) if o.get("type") == "message"
                for c in o.get("content", [])
            )
            return _extract_json(text)
        except (requests.RequestException, ValueError) as exc:
            last_exc = exc
    raise last_exc  # type: ignore[misc]


def _ask(briefing: dict) -> dict:
    """Cached LLM call: one per match, keyed on (version, model, match_id).

    The key deliberately excludes the odds snapshot so the call happens once and
    is reused; ttl=0 freezes the pre-match research (also a leak guard).
    """
    key = _cache_key(briefing["match_id"])
    return cache.get_or_fetch("calibration", key, lambda: _call_llm(briefing), ttl=0)


def _apply(pred, tilt_points: float) -> tuple[int, int]:
    """Apply one tilt to an anchor in logit space; return (new_int, delta).

    Soft weight ``w`` shrinks moves on liquid markets; the per-book ``cap`` is the
    hard bound that ultimately protects deep consensus and lets lone/empirical
    anchors be corrected hard.
    """
    p0 = pred.probability
    n = pred.n_books or 0
    cap = cap_for_books(n)
    spread = _spread(list(pred.book_probabilities or []))
    if n >= 2 and spread is not None:
        w = _clamp(spread / SPREAD_REF, W_MIN, 1.0)   # multi-book: shrink by spread
    elif n == 1:
        w = 1.0                                       # lone book: let the tilt bite
    else:
        w = W_EMPIRICAL                               # n == 0: model estimate
    t = _clamp(tilt_points, -MAX_TILT, MAX_TILT)
    p_new = _sigmoid(_logit(_clamp(p0, 0.001, 0.999)) + ALPHA * w * t)
    p_new = _clamp(p_new, p0 - cap / 100.0, p0 + cap / 100.0)   # hard per-book cap
    new_int = max(1, min(99, round(p_new * 100)))
    return new_int, new_int - pred.probability_int


def calibrate(result, lineups, minutes_before: float | None, *, force: bool = False):
    """Tilt every anchored prediction on ``result`` in place; return ``result``.

    No-op (returns unchanged) when disabled, keyless, after kickoff (leak guard),
    or on any LLM error/timeout — so a failure is never worse than raw anchors.
    """
    if not (force or ENABLED) or not config.OPENAI_API_KEY:
        return result
    if minutes_before is not None and minutes_before <= 0:
        return result  # leak guard: never research a kicked-off / finished match
    if not result.predictions:
        return result

    briefing = build_briefing(result, lineups, minutes_before or 0.0)
    try:
        resp = _ask(briefing) or {}
    except Exception:
        return result  # degrade to anchors on any failure/timeout

    tilts: dict[str, tuple[float, str | None]] = {}
    for item in resp.get("tilts") or []:
        mid = item.get("market_id")
        points = item.get("tilt_points")
        if mid is not None and isinstance(points, (int, float)):
            tilts[mid] = (float(points), item.get("rationale"))

    result.calibration_briefing = resp.get("briefing")
    result.calibration_sources = resp.get("sources") or []

    for pred in result.predictions:
        pred.anchor_probability_int = pred.probability_int
        points, rationale = tilts.get(pred.market_id, (0.0, None))
        if not points:
            pred.tilt_points = 0.0
            pred.applied_delta = 0
            continue
        new_int, delta = _apply(pred, points)
        pred.tilt_points = points
        pred.applied_delta = delta
        pred.calibration_rationale = rationale
        pred.probability_int = new_int
        pred.probability = new_int / 100.0
    return result
