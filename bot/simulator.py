"""Process boundary to the numerical simulator bundled with this bot.

The live-pricing package intentionally keeps its imports tiny. The heavier
learned-rate stack lives under ``simulator/``, so this module invokes its stable
JSON bridge in a child process and returns compact, auditable context for markets
Codex cannot price from a direct bookmaker contract.

The bridge accepts one JSON object on stdin with ``home``/``away``/``questions``
and optional ``kickoff``/``stage``/``referee``/``lineups``/``n_sims``. Schema 2.2 returns
``question_reports`` per supported question — a YES probability, the resolved
``family`` and stable ``contract_key``, a deterministic ``explanation`` and
``historical_evidence`` (exact-contract empirical rates and family-level unseen
Brier comparisons against 50/50 and empirical-rate baselines, with sample sizes
for all-history and WC2026), and
``evidence_role=model_context`` — plus a separate ``unsupported_questions`` list,
which we never turn into estimates. We do not duplicate the simulator's
question-feasibility rules here: the report preflights its baseline and additive
parsers and returns genuinely unsupported templates separately.
"""
from __future__ import annotations

import json
import math
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Iterable

from . import config
from .odds_context import PriceCtx


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

    A market is sent to the simulator only when it has **no exact direct price**.
    The simulator decides
    what it can actually resolve; unsupported templates come back separately and
    are dropped. A missing bundled runtime, missing dependencies, a timeout, or
    a parse error all fail open to an empty result so evidence building stays
    robust.
    """
    targets = _targets(markets, direct_by_market, intents)
    if not targets:
        return {}
    proxy_notes = {
        target["market_id"]: target.pop("simulator_proxy_note")
        for target in targets
        if target.get("simulator_proxy_note")
    }

    runtime = _runtime(simulator_root)
    if runtime is None:
        return {}
    root, python = runtime

    method_targets = _goal_method_targets(targets)
    bridge_targets = list(targets)
    if method_targets:
        bridge_targets.append({
            "market_id": "__goal_method_total_goals_ge1",
            "question": "Will there be at least 1 goal in regulation?",
        })

    payload = _payload(ctx, bridge_targets, kickoff=kickoff, referee=referee,
                       stage=stage, lineups=lineups)
    raw = _run_bridge(payload, root, python)
    estimates = _reports_by_market(raw, proxy_notes=proxy_notes)
    if method_targets:
        estimates.update(_goal_method_estimates(method_targets, estimates))
        estimates.pop("__goal_method_total_goals_ge1", None)
    return estimates


def _targets(
    markets: Iterable[dict],
    direct_by_market: dict[str, list] | None,
    intents: dict[str, dict] | None,
) -> list[dict]:
    """Markets to price with the simulator: every market without exact odds."""
    direct_by_market = direct_by_market or {}
    intents = intents or {}
    targets = []
    for market in markets:
        raw_mid = market["id"]
        mid = str(raw_mid)
        has_direct = bool(direct_by_market.get(raw_mid) or direct_by_market.get(mid))
        if has_direct:
            continue
        intent = intents.get(raw_mid) or intents.get(mid) or {}
        target = {
            "market_id": mid,
            "question": _simulator_question(
                str(market.get("question") or ""), intent,
            ),
        }
        proxy_note = _simulator_proxy_note(intent)
        if proxy_note:
            target["simulator_proxy_note"] = proxy_note
        targets.append(target)
    return targets


def _simulator_question(question: str, intent: dict) -> str:
    """Rewrite known bot intents to simulator-supported canonical wording."""
    if intent.get("market") == "any_team_player_shots_on_target":
        try:
            threshold = int(intent.get("threshold") or 2)
        except (TypeError, ValueError):
            threshold = 2
        return (
            f"Will any player have {threshold} or more shots on target "
            "in regulation?"
        )
    return question


def _simulator_proxy_note(intent: dict) -> str | None:
    if intent.get("market") == "any_team_player_shots_on_target":
        side = intent.get("subject")
        scope = "team-specific" if side in ("home", "away") else "single-team"
        return (
            "Simulator proxy: broad any-player shots-on-target threshold for both "
            f"teams, used as context for the narrower {scope} player threshold. "
            "Do not treat it as an exact direct price."
        )
    return None


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


def _reports_by_market(raw: dict, *, proxy_notes: dict[str, str] | None = None) -> dict[str, dict]:
    """Key supported ``question_reports`` by market id; drop unsupported ones.

    Each value is exactly one report item (the simulator's per-question contract)
    plus compact model provenance and the not-an-anchor reminder. We carry the
    schema-2.2 fields through verbatim: ``contract_key`` (the stable semantic
    key) and ``historical_evidence`` (exact-contract empirical rates plus family
    Brier comparisons against both 50/50 and prior empirical-rate baselines, with
    sample sizes for all-history and WC2026, each scope possibly ``available:
    false``). The evidence builder later projects these internals into the small
    decision-only structure sent to the Codex agent. Match-level internals, other
    questions' probabilities and ``unsupported_questions`` are not attached here.
    """
    raw = raw or {}
    model_meta = raw.get("model") or {}
    # Provenance is identical for every question in a match; build it once.
    model = {
        "engine": model_meta.get("engine"),
        "rate_model": model_meta.get("rate_model"),
        "n_sims": model_meta.get("n_sims"),
    }
    proxy_notes = proxy_notes or {}
    out: dict[str, dict] = {}
    for rep in raw.get("question_reports") or []:
        mid = str(rep.get("market_id") or "")
        prob = _as_probability(rep.get("probability"))
        if not mid or prob is None:
            continue
        proxy_note = proxy_notes.get(mid)
        adjustment_guidance = _simulator_adjustment_guidance(rep)
        if proxy_note:
            adjustment_guidance = f"{proxy_note} {adjustment_guidance}"
        note = (
            "Learned-rate simulator context only; not a final anchor. The "
            "Codex must weigh it against its disclosed conditioning inputs, "
            "lineups, tactics, game state, referee, and market freshness."
        )
        if proxy_note:
            note = f"{proxy_note} {note}"
        item = {
            "source": rep.get("source") or "sportspredict-simulator",
            "family": rep.get("family"),
            "contract_key": rep.get("contract_key"),
            "probability": round(prob, 6),
            "probability_pct": round(prob * 100.0, 2),
            "explanation": rep.get("explanation"),
            "adjustment_guidance": adjustment_guidance,
            "historical_evidence": rep.get("historical_evidence"),
            "evidence_role": rep.get("evidence_role") or "model_context",
            "model": model,
            "note": note,
        }
        if proxy_note:
            item["proxy_note"] = proxy_note
        out[mid] = item
    return out


def _simulator_adjustment_guidance(rep: dict) -> str:
    """Codex-facing directions for a simulator fallback estimate."""
    family = str(rep.get("family") or "").lower()
    question = str(rep.get("question") or "").lower()
    key = str(rep.get("contract_key") or "").lower()
    if "substitution_before_halftime" in family:
        return (
            "Lean toward WC2026 empirical rates; check whether either team has "
            "made first-half substitutions in this tournament. Raise only for "
            "concrete injury/fitness doubts, heat, tactical-disaster risk, "
            "goalkeeper/defender injury risk, concussion concern, or a coach "
            "with recent early changes; ignore normal 60'-75' substitutions."
        )
    if "substitute_score_or_assist" in family:
        return (
            "Lean toward WC2026 empirical rates and modern five-sub context. "
            "Identify each team's likely attacking substitutes and whether they "
            "are real scorers, creators, set-piece takers, or assist threats from "
            "player_form or research."
        )
    if "substitute_score" in family:
        return (
            "Lean toward WC2026 empirical rates and modern five-sub context. "
            "Identify each team's likely attacking substitutes and whether they "
            "are real scorers/shooters from player_form or research."
        )
    if "card" in family or "card" in question:
        return (
            "Compare the simulator with direct card prices, referee discipline, "
            "team foul/card profiles and match stakes. Preserve the stated "
            "regulation or match scope."
        )
    if "penalt" in family or "penalty" in question:
        return (
            "Compare the simulator with direct penalty prices, box-entry volume, "
            "dribblers, VAR-sensitive defending and referee penalty history."
        )
    if "player" in family:
        return (
            "Adjust primarily for confirmed start, expected minutes, role, "
            "position, penalties/set pieces, recent involvement and direct player "
            "prices when available."
        )
    if "shot" in family or "shot" in question:
        return (
            "Compare with direct shot prices, attacking lineups, territorial "
            "dominance and tempo. Be stricter when the simulator has weak "
            "contract-level Brier history."
        )
    if "goal_window" in family and "et" in key:
        return (
            "This simulator probability includes the contract's extra-time "
            "exposure. Use live result and draw markets in the parent pricing "
            "layer to sanity-check that exposure."
        )
    return (
        "Use this as model context only. Prefer exact fresh odds when available, "
        "then compare with lineups, injuries, tactics, referee and match-state "
        "incentives while preserving the stated time scope."
    )


GOAL_METHOD_PRIORS = {
    "header": {
        "contract_key": "goal_method:header:reg",
        "question_label": "header goal scored in regulation",
        "per_goal_probability": 0.179,
        "match_rate": 104 / 314,
        "goal_count": "131 / 730 regulation goals",
    },
    "outside_box": {
        "contract_key": "goal_method:outside_box:reg",
        "question_label": "outside-the-penalty-area goal scored in regulation",
        "per_goal_probability": 0.122,
        "match_rate": 76 / 314,
        "goal_count": "89 / 730 regulation goals",
    },
}


def _goal_method_targets(targets: list[dict]) -> dict[str, str]:
    out = {}
    for target in targets:
        method = _goal_method(str(target.get("question") or ""))
        if method:
            out[str(target["market_id"])] = method
    return out


def _goal_method(question: str) -> str | None:
    lower = question.lower()
    if "header goal be scored" in lower:
        return "header"
    if "goal be scored from outside the penalty area" in lower:
        return "outside_box"
    return None


def _goal_method_estimates(
    method_targets: dict[str, str],
    estimates: dict[str, dict],
) -> dict[str, dict]:
    total = estimates.get("__goal_method_total_goals_ge1") or {}
    p_any_goal = _as_probability(total.get("probability"))
    if p_any_goal is None:
        return {}
    # Treat the simulator's P(any regulation goal) as the zero-count probability
    # of a Poisson-like goal process. Then thinning by a
    # per-goal method Bernoulli gives P(any method goal) = 1-exp(-lambda*p).
    p_any_goal = min(max(p_any_goal, 0.000001), 0.999999)
    goal_lambda = -math.log(1.0 - p_any_goal)
    model = dict(total.get("model") or {})
    model["post_simulation_layer"] = "statsbomb_goal_method_bernoulli"
    out = {}
    for market_id, method in method_targets.items():
        prior = GOAL_METHOD_PRIORS[method]
        per_goal = prior["per_goal_probability"]
        probability = 1.0 - math.exp(-goal_lambda * per_goal)
        out[market_id] = {
            "source": "sportspredict-simulator-goal-method",
            "family": "goal_method",
            "contract_key": prior["contract_key"],
            "probability": round(probability, 6),
            "probability_pct": round(probability * 100.0, 2),
            "explanation": (
                "Goal-method post-simulation estimate: condition on the "
                "simulated probability of at least one regulation goal, then "
                "thin each simulated regulation goal with "
                f"a StatsBomb-calibrated per-goal probability for {prior['question_label']}."
            ),
            "adjustment_guidance": (
                "Use as a conservative fallback when no direct odds exist. The "
                "method label comes from 314 StatsBomb open-data tournament "
                "matches, not the full API-Football learned-rate history; keep "
                "the live goal-total evidence in the parent pricing layer as "
                "the main driver."
            ),
            "historical_evidence": {
                "empirical_rate": {
                    "all_history": {
                        "available": True,
                        "rate": prior["match_rate"],
                        "observations": 314,
                        "population": (
                            "StatsBomb open-data tournament matches with shot "
                            "body-part/location labels retained for goal-method "
                            "markets."
                        ),
                    }
                }
            },
            "conditioning_inputs": {
                "simulated_any_regulation_goal_probability": round(p_any_goal, 6),
                "implied_regulation_goal_lambda": round(goal_lambda, 6),
                "per_goal_method_probability": per_goal,
                "statsbomb_goal_method_sample": prior["goal_count"],
            },
            "evidence_role": "model_context",
            "model": model,
            "note": (
                "Goal-method model context only; not a final anchor. The pricing "
                "Codex should still prefer exact/direct market odds when available "
                "and treat the 314-match StatsBomb label source as a conservative "
                "fallback."
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
    # Preserve a virtualenv launcher path. Resolving its symlink jumps to the
    # base interpreter and silently drops the venv's simulator dependencies.
    python = Path(sys.executable)
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


def _as_probability(value) -> float | None:
    try:
        p = float(value)
    except (TypeError, ValueError):
        return None
    if 0.0 <= p <= 1.0:
        return p
    return None
