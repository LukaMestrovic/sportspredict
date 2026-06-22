"""Layer 4 — external-source estimation (last resort).

When no odds source and no derivation can price a question, fall back to a
web-grounded LLM estimate: the model searches prediction markets, team news,
stats and previews, then returns a calibrated YES probability.

**Cost & quota.** This uses OpenAI web search (~$0.03–0.04 per question — far
more than the nano parser), so every result is cached to disk (keyed by match +
question); a question is searched at most once. Disable with env
`EXTERNAL_FALLBACK=0`. See README "Cost".
"""
from __future__ import annotations

import os
import re

import requests

from . import cache, config

MODEL = os.environ.get("EXTERNAL_MODEL", "gpt-4.1-mini")
ENABLED = os.environ.get("EXTERNAL_FALLBACK", "1") != "0"

_SYS = (
    "You are a calibrated soccer forecaster for a probability competition. "
    "Research the specific match using current web sources — prediction markets, "
    "team/lineup news, injuries, recent form and match previews — then give the "
    "probability that the YES outcome occurs. Be well-calibrated, not bold. "
    "Reply with a one-line rationale, then a final line exactly: 'ANSWER: <N>' "
    "where N is an integer 1-99."
)


def _ask(prompt: str) -> float | None:
    r = requests.post(
        "https://api.openai.com/v1/responses",
        headers={"Authorization": f"Bearer {config.OPENAI_API_KEY}"},
        json={"model": MODEL, "tools": [{"type": "web_search_preview"}],
              "input": f"{_SYS}\n\n{prompt}"},
        timeout=120,
    )
    r.raise_for_status()
    d = r.json()
    txt = "".join(
        c.get("text", "")
        for o in d.get("output", []) if o.get("type") == "message"
        for c in o.get("content", [])
    )
    m = re.search(r"ANSWER:\s*(\d{1,2})", txt)
    if not m:
        return None
    n = int(m.group(1))
    return max(1, min(99, n)) / 100.0


def estimate(question: str, home: str, away: str, kickoff: str):
    """Return (out, 'external') or (None, None)."""
    if not (ENABLED and config.OPENAI_API_KEY):
        return None, None
    key = f"{home}|{away}|{kickoff[:10]}|{question}"
    prompt = (f"Match: {home} vs {away} (FIFA World Cup 2026, kickoff {kickoff}).\n"
              f"Question (answer YES probability): {question}")
    try:
        p = cache.get_or_fetch("external", key, lambda: _ask(prompt), ttl=0)
    except requests.HTTPError:
        return None, None
    if p is None:
        return None, None
    return {"probability": p, "n_books": 0, "label": "web-estimate"}, "external"
