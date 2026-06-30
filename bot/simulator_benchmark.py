"""Tournament-wide WC2026 simulator benchmark on settled questions.

The learned artifacts were fitted before WC2026.  A tracked replay seed covers
the settled tournament at build time; after deployment, each newly settled
match is replayed once with the same frozen simulator and retained in cache.
This measures the simulator itself, without using LLM prices or reasoning.
"""

from __future__ import annotations

import copy
import json
import math
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from . import config, simulator
from .pricing import PriceCtx


SNAPSHOT_PATH = config.ROOT / "cache" / "simulator_family_benchmark.json"
REPLAY_DIR = config.ROOT / "cache" / "wc2026_simulator_replay"
SEED_PATH = (
    config.ROOT / "simulator" / "data" / "processed"
    / "wc2026_simulator_replay_seed.json"
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _seed() -> dict:
    try:
        return json.loads(SEED_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return {"rows": []}


def _cached_rows() -> tuple[list[dict], set[str]]:
    rows = []
    match_ids = set()
    if not REPLAY_DIR.is_dir():
        return rows, match_ids
    for path in sorted(REPLAY_DIR.glob("*.json")):
        try:
            replay = json.loads(path.read_text())
            match_id = str(replay.get("match_id") or path.stem)
            match_ids.add(match_id)
            rows.extend(replay.get("rows") or [])
        except (OSError, json.JSONDecodeError):
            continue
    return rows, match_ids


def _team_name(match: dict, side: str) -> str | None:
    competitors = match.get(f"{side}_competitors") or []
    return competitors[0].get("name") if competitors else None


def _stage(match: dict) -> str:
    label = str(match.get("season_type") or "").lower()
    return "group" if "group" in label else "knockout"


def _replay_match(match: dict, markets: list[dict]) -> dict:
    home = _team_name(match, "home")
    away = _team_name(match, "away")
    if not home or not away:
        return {"match_id": match.get("id"), "rows": [], "error": "missing team names"}
    settled = [
        market for market in markets
        if market.get("current_value") in (0, 100)
        and market.get("question")
    ]
    targets = [
        {"id": str(market["id"]), "question": str(market["question"])}
        for market in settled
    ]
    estimates = simulator.simulator_estimates(
        targets,
        PriceCtx(home, away, [], None, None),
        direct_by_market={market["id"]: [] for market in targets},
        kickoff=match.get("opening_time"),
        stage=_stage(match),
    )
    rows = []
    for market in settled:
        estimate = estimates.get(str(market["id"]))
        if not estimate:
            continue
        history = estimate.get("historical_evidence") or {}
        empirical = (history.get("empirical_rate") or {}).get("all_history") or {}
        p_empirical = empirical.get("rate") if empirical.get("available") else None
        rows.append({
            "match_id": str(match["id"]),
            "kickoff": match.get("opening_time"),
            "family": str(estimate.get("family") or "unknown"),
            "contract_key": estimate.get("contract_key"),
            "p_model": float(estimate["probability"]),
            "p_empirical": float(p_empirical) if p_empirical is not None else None,
            "outcome": int(market["current_value"]) // 100,
        })
    return {
        "schema_version": 1,
        "generated_at": _now(),
        "match_id": str(match["id"]),
        "match_name": match.get("name"),
        "kickoff": match.get("opening_time"),
        "rows": rows,
    }


def _write_replay(match_id: str, replay: dict) -> None:
    REPLAY_DIR.mkdir(parents=True, exist_ok=True)
    path = REPLAY_DIR / f"{match_id}.json"
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(replay, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    temporary.replace(path)


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
        guidance = "Use only as a weak directional check; the tournament sample is small."
    elif matches < 200:
        level = "moderate"
        guidance = "Use as supporting evidence with contract and match-specific checks."
    else:
        level = "large"
        guidance = "Use as a meaningful family-level reliability signal."
    return {
        "level": level,
        "basis": "unique settled WC2026 matches in the frozen-model replay",
        "guidance": guidance,
    }


def _summaries(rows: list[dict]) -> dict[str, dict]:
    family_totals: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        family_totals[row["family"]].append(row)
    summaries = {}
    for family, all_family_rows in sorted(family_totals.items()):
        family_rows = [
            row for row in all_family_rows if row.get("p_empirical") is not None
        ]
        if not family_rows:
            questions = len(all_family_rows)
            matches = len({row["match_id"] for row in all_family_rows})
            model_brier = sum(
                (row["p_model"] - row["outcome"]) ** 2 for row in all_family_rows
            ) / questions
            summaries[family] = {
                "available": True,
                "scope": "wc2026_frozen_pre2026_simulator_replay",
                "family": family,
                "evaluation": (
                    "Frozen pre-2026 learned simulator replayed on settled WC2026 "
                    "questions. This is simulator-only performance. No pre-2026 "
                    "exact-contract empirical baseline is available for this family."
                ),
                "questions": questions,
                "matches": matches,
                "contracts": len({row["contract_key"] for row in all_family_rows}),
                "coverage": {
                    "comparable_questions": 0,
                    "simulator_questions": questions,
                },
                "brier": {
                    "simulator": round(model_brier, 6),
                    "always_50": 0.25,
                },
                "comparison_signal": "empirical_baseline_unavailable",
                "sample_size": _sample_size(matches),
            }
            continue
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
            "scope": "wc2026_frozen_pre2026_simulator_replay",
            "family": family,
            "evaluation": (
                "Frozen pre-2026 learned simulator replayed on settled WC2026 questions. "
                "This is simulator-only performance, not LLM-layer performance. The "
                "baseline always predicts the pre-WC2026 exact-contract empirical rate."
            ),
            "questions": questions,
            "matches": matches,
            "contracts": len({row["contract_key"] for row in family_rows}),
            "coverage": {
                "comparable_questions": questions,
                "simulator_questions": len(all_family_rows),
            },
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


def _contract_rates(rows: list[dict]) -> dict[str, dict]:
    grouped: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        if row.get("contract_key"):
            grouped[str(row["contract_key"])].append(row)
    result = {}
    for key, contract_rows in sorted(grouped.items()):
        result[key] = {
            "available": True,
            "basis": "settled SportPredict WC2026 question instances",
            "population": "settled_question_instances",
            "yes_events": sum(int(row["outcome"]) for row in contract_rows),
            "observations": len(contract_rows),
            "matches": len({row["match_id"] for row in contract_rows}),
            "rate": round(
                sum(int(row["outcome"]) for row in contract_rows) / len(contract_rows), 6,
            ),
            "data_through": max(
                (str(row.get("kickoff") or "") for row in contract_rows), default=None,
            ),
            "complete": True,
        }
    return result


def refresh(sp, web, event_id: str, lobby_id: str, *, path: Path = SNAPSHOT_PATH) -> dict:
    """Replay newly settled matches and atomically rebuild the tournament benchmark."""
    seed = _seed()
    seed_rows = list(seed.get("rows") or [])
    seed_matches = {str(row["match_id"]) for row in seed_rows}
    cached_rows, cached_matches = _cached_rows()
    cached_rows = [
        row for row in cached_rows if str(row["match_id"]) not in seed_matches
    ]
    existing = seed_rows + cached_rows
    known_matches = seed_matches | cached_matches
    settled_matches = web.settled_matches(event_id, refresh=True)
    replayed = 0
    for match in settled_matches:
        match_id = str(match["id"])
        if match_id in known_matches:
            continue
        replay = _replay_match(match, web.settled_crowd_stats(match_id, lobby_id))
        _write_replay(match_id, replay)
        existing.extend(replay.get("rows") or [])
        known_matches.add(match_id)
        replayed += 1

    snapshot = {
        "schema_version": 2,
        "generated_at": _now(),
        "evaluation": "simulator_only_frozen_pre2026_wc2026_replay",
        "settled_tournament_matches": len(settled_matches),
        "replayed_matches": len({row["match_id"] for row in existing}),
        "simulator_questions": len(existing),
        "comparable_simulator_questions": sum(
            row.get("p_empirical") is not None for row in existing
        ),
        "new_matches_replayed": replayed,
        "families": _summaries(existing),
        "contracts": _contract_rates(existing),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    temporary.replace(path)
    return snapshot


def load(path: Path = SNAPSHOT_PATH) -> dict:
    try:
        snapshot = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return snapshot if snapshot.get("schema_version") == 2 else {}


def overlay(estimates: dict[str, dict], snapshot: dict) -> dict[str, dict]:
    """Replace stale tournament scopes with the current simulator-only replay."""
    families = snapshot.get("families") or {}
    contracts = snapshot.get("contracts") or {}
    for estimate in estimates.values():
        history = copy.deepcopy(estimate.get("historical_evidence") or {})
        family = str(estimate.get("family") or "unknown")
        performance = history.setdefault("family_performance", {"family": family})
        performance.pop("live_wc2026", None)
        if family in families:
            performance["wc2026"] = copy.deepcopy(families[family])

        key = estimate.get("contract_key")
        question_rate = contracts.get(key)
        empirical = history.setdefault("empirical_rate", {})
        current = empirical.get("wc2026") or {}
        # Prefer all-labelable API data when available. Otherwise replace the
        # stale shipped tournament slice with current settled question instances.
        if question_rate and current.get("population") != "all_labelable_matches":
            empirical["wc2026"] = copy.deepcopy(question_rate)
        estimate["historical_evidence"] = history
    return estimates
