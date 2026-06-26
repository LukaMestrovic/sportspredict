"""Optional bridge to the sibling sportspredict-hybrid simulator.

The LLM bot intentionally keeps its runtime dependencies tiny. The hybrid bot
has the heavier learned-rate simulator stack, so this module calls it in its own
virtualenv when that sibling checkout is available and returns auditable context
for the penalty markets that have no direct bookmaker contract here.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Iterable

from . import config, derive
from .pricing import PriceCtx


DEFAULT_HYBRID_ROOT = config.ROOT.parent / "sportspredict-hybrid"
TIMEOUT_SECONDS = 90


_BRIDGE_CODE = r"""
import json
import sys

payload = json.load(sys.stdin)

from sportspredict.config import default_settings
from sportspredict.compete.runner import MatchContextBuilder
from sportspredict.features.context import MatchContext

try:
    from sportspredict.ingest.elo import load_elo_table
except Exception:
    load_elo_table = None

from sphybrid.engine import build_engine

settings = default_settings()
elo_table = {}
elo_path = settings.path("data/raw/elo.csv")
if load_elo_table is not None:
    try:
        elo_table = load_elo_table(elo_path)
    except Exception:
        elo_table = {}

tournament = settings.raw.get("tournament", {})
builder = MatchContextBuilder(
    elo_table=elo_table,
    host_teams=set(tournament.get("host_teams", [])),
    knockout_after=tournament.get("group_stage_end"),
)
match = {
    "name": f"{payload['home']} vs {payload['away']}",
    "opening_time": payload.get("kickoff") or "",
}
try:
    ctx = builder.build(match)
except Exception:
    ctx = MatchContext(
        payload["home"],
        payload["away"],
        stage=payload.get("stage") or "group",
        date=payload.get("kickoff"),
    )

if payload.get("referee"):
    ctx.referee = payload["referee"]

engine = build_engine(settings)
predictions = engine.predict_many(
    ctx,
    [item["question"] for item in payload["questions"]],
    market_odds=payload.get("market_odds") or None,
    n_sims=payload.get("n_sims"),
)

out = {
    "engine": type(engine).__name__,
    "rate_model": type(engine.rate_model).__name__,
    "context": {
        "team_a": ctx.team_a,
        "team_b": ctx.team_b,
        "elo_a": ctx.elo_a,
        "elo_b": ctx.elo_b,
        "stage": ctx.stage,
        "host_a": ctx.host_a,
        "host_b": ctx.host_b,
        "referee": ctx.referee,
    },
    "predictions": [],
}
for item, pred in zip(payload["questions"], predictions):
    out["predictions"].append({
        "market_id": item["market_id"],
        "question": item["question"],
        "market": pred.market,
        "params": pred.params,
        "p_model": pred.p_model,
        "p_final": pred.p_final,
        "p_market": pred.p_market,
        "n_sims": pred.n_sims,
        "notes": pred.notes,
    })
print(json.dumps(out, sort_keys=True))
"""


def simulator_estimates(
    markets: Iterable[dict],
    ctx: PriceCtx,
    *,
    kickoff: str | None = None,
    referee: str | None = None,
    hybrid_root: Path | None = None,
) -> dict[str, dict]:
    """Return hybrid learned-rate simulator estimates keyed by market id.

    Only the two requested penalty question families are sent to the sibling
    simulator. Missing dependencies, a missing sibling checkout, or parse errors
    produce an empty result so evidence building remains robust.
    """
    targets = [
        {"market_id": str(m["id"]), "question": str(m.get("question") or "")}
        for m in markets
        if penalty_market_kind(str(m.get("question") or ""))
    ]
    if not targets:
        return {}

    root = _hybrid_root(hybrid_root)
    python = root / ".venv" / "bin" / "python"
    source = root / "src"
    if not (python.exists() and source.exists()):
        return {}

    payload = {
        "home": ctx.home,
        "away": ctx.away,
        "kickoff": kickoff,
        "referee": referee,
        "questions": targets,
        "market_odds": _market_odds_from_ctx(ctx),
        "n_sims": _n_sims_override(),
    }
    env = os.environ.copy()
    env["PYTHONPATH"] = (
        str(source)
        if not env.get("PYTHONPATH")
        else f"{source}{os.pathsep}{env['PYTHONPATH']}"
    )
    try:
        proc = subprocess.run(
            [str(python), "-c", _BRIDGE_CODE],
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
        raw = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {}

    context = raw.get("context") or {}
    out: dict[str, dict] = {}
    for pred in raw.get("predictions") or []:
        mid = str(pred.get("market_id") or "")
        p_final = _as_probability(pred.get("p_final"))
        if not mid or p_final is None:
            continue
        p_model = _as_probability(pred.get("p_model"))
        out[mid] = {
            "source": "sportspredict-hybrid",
            "model": raw.get("rate_model") or "unknown",
            "engine": raw.get("engine") or "unknown",
            "simulator": "sportspredict.simulate",
            "market": pred.get("market"),
            "params": pred.get("params") or {},
            "probability": round(p_final, 6),
            "probability_pct": round(p_final * 100, 2),
            "p_model": round(p_model, 6) if p_model is not None else None,
            "p_market": pred.get("p_market"),
            "n_sims": pred.get("n_sims"),
            "notes": pred.get("notes"),
            "context": context,
            "odds_anchor_inputs": payload["market_odds"],
            "note": (
                "Learned-rate simulator context only for this unsupported "
                "penalty market; not a final anchor."
            ),
        }
    return out


def penalty_market_kind(question: str) -> str | None:
    """Classify the two penalty market wordings currently supported."""
    lower = question.lower()
    if "penalty kick be awarded" not in lower:
        return None
    if "red card" in lower and re.search(r"\bor\b", lower):
        return "penalty_or_red"
    if "red card" not in lower:
        return "penalty_awarded"
    return None


def _hybrid_root(root: Path | None = None) -> Path:
    raw = os.environ.get("SPORTSPREDICT_HYBRID_ROOT")
    if root is not None:
        return root.expanduser().resolve()
    return (Path(raw) if raw else DEFAULT_HYBRID_ROOT).expanduser().resolve()


def _n_sims_override() -> int | None:
    raw = os.environ.get("SPORTSPREDICT_HYBRID_N_SIMS")
    if not raw:
        return None
    try:
        return max(1, int(raw))
    except ValueError:
        return None


def _market_odds_from_ctx(ctx: PriceCtx) -> dict:
    """Extract no-extra-spend API-Football anchors for the hybrid simulator."""
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
