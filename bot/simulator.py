"""Process boundary to the numerical simulator bundled with this bot.

The live-pricing package intentionally keeps its imports tiny. The heavier
learned-rate stack lives under ``simulator/``, so this module invokes its stable
JSON bridge in a child process and returns compact, auditable context for markets
the LLM cannot price from a direct bookmaker contract.

The bridge accepts one JSON object on stdin with ``home``/``away``/``questions``
and optional ``kickoff``/``stage``/
``referee``/``lineups``/``market_odds``/``n_sims``. Schema 2.0 returns
``question_reports`` per supported question — a YES probability, the resolved
``family`` and stable ``contract_key``, a deterministic ``explanation`` and
``adjustment_guidance``, ``historical_evidence`` (rolling-origin Brier and
empirical rates with sample sizes, all-history and WC2026), and
``evidence_role=model_context`` — plus a separate ``unsupported_questions`` list,
which we never turn into estimates. We do not duplicate the simulator's
question-feasibility rules here: the report preflights its baseline and additive
parsers and returns genuinely unsupported templates separately.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Iterable

from . import config, derive
from .pricing import PriceCtx


SIMULATOR_ROOT = config.ROOT / "simulator"
TIMEOUT_SECONDS = 120


def simulator_estimates(
    markets: Iterable[dict],
    ctx: PriceCtx,
    *,
    direct_by_market: dict[str, list],
    intents: dict[str, dict] | None = None,
    kickoff: str | None = None,
    referee: str | None = None,
    stage: str | None = None,
    lineups: list[dict] | None = None,
    simulator_root: Path | None = None,
) -> dict[str, dict]:
    """Return learned-rate simulator estimates keyed by market id.

    A market is sent to the simulator when it has **no exact direct price**, plus
    the model-sensitive penalty/shot-on-target families we keep even when a
    direct price exists (see :func:`model_estimate_kind`). The simulator decides
    what it can actually resolve; unsupported templates come back separately and
    are dropped. A missing bundled runtime, missing dependencies, a timeout, or
    a parse error all fail open to an empty result so evidence building stays
    robust.
    """
    targets = _targets(markets, direct_by_market, intents)
    if not targets:
        return {}

    runtime = _runtime(simulator_root)
    if runtime is None:
        return {}
    root, python = runtime

    payload = _payload(ctx, targets, kickoff=kickoff, referee=referee,
                       stage=stage, lineups=lineups)
    raw = _run_bridge(payload, root, python)
    return _reports_by_market(raw)


def _targets(
    markets: Iterable[dict],
    direct_by_market: dict[str, list] | None,
    intents: dict[str, dict] | None,
) -> list[dict]:
    """Markets to price with the simulator: every no-direct market, plus the
    retained model-sensitive penalty/shot-on-target targets even with direct odds."""
    direct_by_market = direct_by_market or {}
    intents = intents or {}
    targets = []
    for market in markets:
        raw_mid = market["id"]
        mid = str(raw_mid)
        question = str(market.get("question") or "")
        has_direct = bool(direct_by_market.get(raw_mid) or direct_by_market.get(mid))
        model_sensitive = model_estimate_kind(question, intents.get(mid) or intents.get(raw_mid)) is not None
        if has_direct and not model_sensitive:
            continue
        targets.append({"market_id": mid, "question": question})
    return targets


def _payload(
    ctx: PriceCtx,
    targets: list[dict],
    *,
    kickoff: str | None,
    referee: str | None,
    stage: str | None,
    lineups: list[dict] | None,
) -> dict:
    payload = {
        "home": ctx.home,
        "away": ctx.away,
        "kickoff": kickoff,
        "referee": referee,
        "stage": stage,
        "questions": targets,
        "market_odds": _market_odds_from_ctx(ctx),
        "n_sims": _n_sims_override(),
    }
    # The simulator understands the native API-Football lineups response and
    # uses it only for player/substitute allocation. Send it as-is when present.
    if lineups:
        payload["lineups"] = lineups
    return payload


def _run_bridge(payload: dict, root: Path, python: Path) -> dict:
    """Invoke the bundled JSON bridge; fail open to ``{}``."""
    source = root / "src"
    env = os.environ.copy()
    env["PYTHONPATH"] = (
        str(source)
        if not env.get("PYTHONPATH")
        else f"{source}{os.pathsep}{env['PYTHONPATH']}"
    )
    # Pin artifact resolution to this tracked runtime instead of relying on cwd.
    env["SPORTSPREDICT_ROOT"] = str(root)
    try:
        proc = subprocess.run(
            [str(python), "-m", "sphybrid.bridge"],
            cwd=str(root),
            env=env,
            input=json.dumps(payload),
            text=True,
            capture_output=True,
            timeout=TIMEOUT_SECONDS,
            check=False,
        )
    except Exception:
        return {}
    if proc.returncode != 0:
        return {}
    try:
        return json.loads(proc.stdout)
    except (json.JSONDecodeError, TypeError):
        return {}


def _reports_by_market(raw: dict) -> dict[str, dict]:
    """Key supported ``question_reports`` by market id; drop unsupported ones.

    Each value is exactly one report item (the simulator's per-question contract)
    plus compact model provenance and the not-an-anchor reminder. We carry the
    schema-2.0 fields through verbatim: ``contract_key`` (the stable semantic
    key), ``adjustment_guidance`` (deterministic pre-match directions), and
    ``historical_evidence`` (rolling-origin Brier and empirical rates with their
    sample sizes, all-history and WC2026, each scope possibly ``available:
    false``) — the pricing LLM needs those scopes and counts intact to weight the
    estimate. Match-level internals, other questions' probabilities and
    ``unsupported_questions`` are intentionally not attached.
    """
    raw = raw or {}
    model_meta = raw.get("model") or {}
    # Provenance is identical for every question in a match; build it once.
    model = {
        "engine": model_meta.get("engine"),
        "rate_model": model_meta.get("rate_model"),
        "n_sims": model_meta.get("n_sims"),
        "odds_anchor_applied": model_meta.get("odds_anchor_applied"),
    }
    out: dict[str, dict] = {}
    for rep in raw.get("question_reports") or []:
        mid = str(rep.get("market_id") or "")
        prob = _as_probability(rep.get("probability"))
        if not mid or prob is None:
            continue
        out[mid] = {
            "source": rep.get("source") or "sportspredict-simulator",
            "family": rep.get("family"),
            "contract_key": rep.get("contract_key"),
            "probability": round(prob, 6),
            "probability_pct": round(prob * 100.0, 2),
            "explanation": rep.get("explanation"),
            "adjustment_guidance": rep.get("adjustment_guidance"),
            "historical_evidence": rep.get("historical_evidence"),
            "evidence_role": rep.get("evidence_role") or "model_context",
            "model": model,
            "note": (
                "Learned-rate simulator context only; not a final anchor. The "
                "pricing LLM must weigh it against direct odds, related odds, "
                "lineups, tactics, game state, referee, and market freshness."
            ),
        }
    return out


def model_estimate_kind(question: str, intent: dict | None = None) -> str | None:
    """Classify the model-sensitive penalty/shot-on-target families that are sent
    to the simulator even when an exact direct price exists.

    This is intentionally a small, curated set — not an exhaustive allowlist of
    everything the simulator supports. The bundled report preflights feasibility
    for all other (no-direct) questions, so new feasible templates are accepted
    without being enumerated here.
    """
    penalty = _penalty_market_kind(question)
    if penalty:
        return penalty

    lower = question.lower()
    intent = intent or {}
    market = intent.get("market")
    period = intent.get("period", "match")
    subject = intent.get("subject")

    if (
        market == "shots_on_target_compare"
        and period == "2H"
        and subject in ("home", "away")
        and "more shots on target than" in lower
    ):
        return "team_more_shots_on_target_2h"

    if (
        market == "team_shots_on_target"
        and period == "match"
        and subject in ("home", "away")
        and intent.get("comparator") == "gte"
        and intent.get("threshold") is not None
    ):
        return "team_shots_on_target_threshold"

    if (
        "both teams" in lower
        and "shot on target" in lower
        and ("at least 1" in lower or "1 or more" in lower)
        and period in ("1H", "2H")
    ):
        return f"both_teams_shot_on_target_{period.lower()}"

    return None


def _penalty_market_kind(question: str) -> str | None:
    """Classify the two penalty market wordings currently supported."""
    lower = question.lower()
    if "penalty kick be awarded" not in lower:
        return None
    if "red card" in lower and re.search(r"\bor\b", lower):
        return "penalty_or_red"
    if "red card" not in lower:
        return "penalty_awarded"
    return None


def _runtime(root: Path | None = None) -> tuple[Path, Path] | None:
    """Return the tracked simulator root and this process's Python executable."""
    resolved = (root or SIMULATOR_ROOT).expanduser().resolve()
    python = Path(sys.executable).resolve()
    if (resolved / "src" / "sphybrid" / "bridge.py").is_file() and python.is_file():
        return resolved, python
    return None


def _n_sims_override() -> int | None:
    raw = os.environ.get("SPORTSPREDICT_SIMULATOR_N_SIMS")
    if not raw:
        return None
    try:
        return max(1, int(raw))
    except ValueError:
        return None


def _market_odds_from_ctx(ctx: PriceCtx) -> dict:
    """Extract no-extra-spend API-Football anchors for the bundled simulator."""
    out: dict = {}
    total_goals = derive._infer_total_rate(ctx.af_books, 5)
    total_cards = derive._infer_total_rate(ctx.af_books, 80)
    home_goals = derive._infer_total_rate(ctx.af_books, 16)
    away_goals = derive._infer_total_rate(ctx.af_books, 17)
    if total_goals is not None:
        out["total_goals_mean"] = round(total_goals, 6)
    if total_cards is not None:
        out["total_cards"] = round(total_cards, 6)
    if home_goals is not None and away_goals is not None:
        out["team_goals"] = [round(home_goals, 6), round(away_goals, 6)]
    return out


def _as_probability(value) -> float | None:
    try:
        p = float(value)
    except (TypeError, ValueError):
        return None
    if 0.0 <= p <= 1.0:
        return p
    return None
