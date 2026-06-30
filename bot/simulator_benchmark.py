"""Live WC2026 family benchmark from frozen pre-match evidence and ledger outcomes."""

from __future__ import annotations

import copy
import json
import math
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from . import config


SNAPSHOT_PATH = config.ROOT / "cache" / "simulator_family_benchmark.json"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _evidence_path(raw: str) -> Path | None:
    path = Path(raw)
    candidates = [path, config.ROOT / path]
    if "logs" in path.parts:
        candidates.append(config.ROOT / Path(*path.parts[path.parts.index("logs"):]))
    return next((candidate for candidate in candidates if candidate.is_file()), None)


def _settled_rows(ledger_path: Path) -> list[dict]:
    if not ledger_path.is_file():
        return []
    db = sqlite3.connect(ledger_path)
    db.row_factory = sqlite3.Row
    try:
        rows = db.execute(
            """SELECT r.match_id, r.recorded_at, r.evidence_path,
                      q.market_id, q.outcome
                 FROM questions q JOIN runs r ON r.id = q.run_id
                WHERE r.status = 'submitted' AND q.outcome IS NOT NULL
                  AND r.evidence_path IS NOT NULL
                ORDER BY r.recorded_at"""
        ).fetchall()
    except sqlite3.DatabaseError:
        return []
    finally:
        db.close()
    # The last submitted pre-match run is the final frozen model view of a market.
    latest = {(row["match_id"], row["market_id"]): dict(row) for row in rows}
    return list(latest.values())


def _benchmark_rows(ledger_path: Path) -> tuple[list[dict], int]:
    settled = _settled_rows(ledger_path)
    evidence_cache: dict[Path, dict] = {}
    benchmark = []
    for row in settled:
        path = _evidence_path(row["evidence_path"])
        if path is None:
            continue
        if path not in evidence_cache:
            try:
                evidence_cache[path] = json.loads(path.read_text())
            except (OSError, json.JSONDecodeError):
                evidence_cache[path] = {}
        questions = {
            str(item.get("market_id")): item
            for item in evidence_cache[path].get("question_evidence", [])
        }
        item = questions.get(str(row["market_id"])) or {}
        estimates = item.get("simulator_model_estimates") or []
        if not estimates:
            continue
        estimate = estimates[0]
        history = estimate.get("historical_evidence") or {}
        empirical = (history.get("empirical_rate") or {}).get("all_history") or {}
        try:
            p_model = float(estimate["probability"])
            p_empirical = float(empirical["rate"])
            outcome = int(row["outcome"])
        except (KeyError, TypeError, ValueError):
            continue
        if not (0 <= p_model <= 1 and 0 <= p_empirical <= 1 and outcome in (0, 1)):
            continue
        benchmark.append({
            "family": str(estimate.get("family") or "unknown"),
            "contract_key": estimate.get("contract_key"),
            "match_id": row["match_id"],
            "p_model": p_model,
            "p_empirical": p_empirical,
            "outcome": outcome,
        })
    return benchmark, len(settled)


def _clustered_ci(rows: list[dict]) -> list[float] | None:
    differences = [
        (
            row["match_id"],
            (row["p_model"] - row["outcome"]) ** 2
            - (row["p_empirical"] - row["outcome"]) ** 2,
        )
        for row in rows
    ]
    clusters: dict[str, list[float]] = defaultdict(list)
    for match_id, value in differences:
        clusters[match_id].append(value)
    if len(differences) < 2 or len(clusters) < 2:
        return None
    mean = sum(value for _, value in differences) / len(differences)
    sums = [sum(value - mean for value in values) for values in clusters.values()]
    variance = (
        len(clusters) / (len(clusters) - 1)
        * sum(value * value for value in sums)
        / (len(differences) ** 2)
    )
    margin = 1.96 * math.sqrt(max(variance, 0.0))
    return [round(mean - margin, 6), round(mean + margin, 6)]


def _sample_size(matches: int) -> dict:
    if matches < 30:
        level = "too_small"
        guidance = "Treat as inconclusive; do not choose the simulator or empirical rate from it."
    elif matches < 75:
        level = "limited"
        guidance = "Use only as a weak directional check; the live tournament sample is small."
    elif matches < 200:
        level = "moderate"
        guidance = "Use as supporting evidence with contract and match-specific checks."
    else:
        level = "large"
        guidance = "Use as a meaningful family-level reliability signal."
    return {
        "level": level,
        "basis": "unique matches settled from frozen pre-match evidence",
        "guidance": guidance,
    }


def _summaries(rows: list[dict]) -> dict[str, dict]:
    grouped: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        grouped[row["family"]].append(row)
    summaries = {}
    for family, family_rows in sorted(grouped.items()):
        questions = len(family_rows)
        matches = len({row["match_id"] for row in family_rows})
        model_brier = sum(
            (row["p_model"] - row["outcome"]) ** 2 for row in family_rows
        ) / questions
        empirical_brier = sum(
            (row["p_empirical"] - row["outcome"]) ** 2 for row in family_rows
        ) / questions
        ci = _clustered_ci(family_rows)
        sample = _sample_size(matches)
        if sample["level"] == "too_small":
            signal = "inconclusive_small_sample"
        elif ci and ci[1] < 0:
            signal = "simulator_better"
        elif ci and ci[0] > 0:
            signal = "empirical_rate_better"
        else:
            signal = "inconclusive"
        summaries[family] = {
            "available": True,
            "scope": "live_wc2026_frozen_predictions",
            "family": family,
            "evaluation": (
                "Actual simulator probabilities frozen before kickoff and later settled only from "
                "explicit SportPredict outcomes; the baseline is the all-history exact-contract "
                "empirical rate frozen in the same evidence file."
            ),
            "questions": questions,
            "matches": matches,
            "contracts": len({row["contract_key"] for row in family_rows}),
            "brier": {
                "simulator": round(model_brier, 6),
                "always_50": 0.25,
                "empirical_rate": round(empirical_brier, 6),
            },
            "delta_brier": {
                "simulator_minus_always_50": round(model_brier - 0.25, 6),
                "simulator_minus_empirical_rate": round(model_brier - empirical_brier, 6),
                "negative_favors_simulator": True,
            },
            "simulator_minus_empirical_rate_95pct_ci": ci,
            "comparison_signal": signal,
            "sample_size": sample,
        }
    return summaries


def refresh(ledger_path: Path, *, path: Path = SNAPSHOT_PATH) -> dict:
    """Rebuild the live benchmark atomically from durable settled ledger rows."""
    rows, settled_rows = _benchmark_rows(ledger_path)
    snapshot = {
        "schema_version": 1,
        "generated_at": _now(),
        "settled_ledger_questions": settled_rows,
        "comparable_simulator_questions": len(rows),
        "matches": len({row["match_id"] for row in rows}),
        "families": _summaries(rows),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    temporary.replace(path)
    return snapshot


def load(path: Path = SNAPSHOT_PATH) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def overlay(estimates: dict[str, dict], snapshot: dict) -> dict[str, dict]:
    """Attach the applicable live family scope to simulator estimates."""
    families = snapshot.get("families") or {}
    for estimate in estimates.values():
        history = copy.deepcopy(estimate.get("historical_evidence") or {})
        family = str(estimate.get("family") or "unknown")
        performance = history.setdefault("family_performance", {"family": family})
        performance["live_wc2026"] = copy.deepcopy(
            families.get(family)
            or {
                "available": False,
                "reason": "No settled frozen simulator predictions for this family yet.",
            }
        )
        performance["live_refresh"] = {
            key: snapshot.get(key)
            for key in (
                "generated_at", "settled_ledger_questions",
                "comparable_simulator_questions", "matches",
            )
        }
        estimate["historical_evidence"] = history
    return estimates
