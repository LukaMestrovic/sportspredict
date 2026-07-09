"""Exhaustive frozen-simulator benchmark on settled WC2026 fixtures.

Unlike the live SportPredict inventory, this evaluator applies every supported
exact contract to every labelable tournament fixture. Team-relative contracts
produce home and away observations; match contracts produce one observation.
Named-player contracts are intentionally excluded because selecting players
from post-match participation would leak the outcome population.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from . import config, simulator, wc2026_evidence
from .odds_context import PriceCtx
from .simulator_contracts import observation_unit, questions_for_contract


SNAPSHOT_PATH = config.ROOT / "cache" / "simulator_family_benchmark.json"
REPLAY_DIR = config.ROOT / "cache" / "wc2026_simulator_contract_replay"
ARTIFACT_PATH = (
    config.ROOT / "simulator" / "data" / "processed" / "simulation_evidence.json"
)
SCHEMA_VERSION = 4
REPLAY_VERSION = 3
LATE_HYDRATION_GOAL_REG_CONTRACT = "goal_window:after_second_hydration:reg"
SHRUNK_EMPIRICAL_RATE_SOURCE = "shrunk_empirical_rate"
LATE_HYDRATION_GOAL_EFFECTIVE_HISTORY = 300


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _artifact() -> dict:
    try:
        return json.loads(ARTIFACT_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return {"contracts": {}}


def _benchmark_contracts(artifact: dict) -> dict[str, dict]:
    contracts = {}
    for key, record in (artifact.get("contracts") or {}).items():
        if not wc2026_evidence.supports_contract(key):
            continue
        if not questions_for_contract(key, "Home", "Away"):
            continue
        empirical = ((record.get("empirical_rate") or {}).get("all_history") or {})
        contracts[key] = {
            "p_empirical": (
                float(empirical["rate"])
                if empirical.get("available") and empirical.get("rate") is not None
                else None
            ),
            "empirical_training_observations": empirical.get("observations"),
        }
    return contracts


def _catalog_hash(contracts: dict[str, dict]) -> str:
    payload = [
        (key, record.get("p_empirical"), questions_for_contract(key, "Home", "Away"))
        for key, record in sorted(contracts.items())
    ]
    return hashlib.sha256(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    ).hexdigest()[:16]


def _team_name(fixture: dict, side: str) -> str | None:
    return str(((fixture.get("teams") or {}).get(side) or {}).get("name") or "") or None


def _replay_path(fixture_id: int) -> Path:
    return REPLAY_DIR / f"{fixture_id}.json"


def _refresh_replay_baselines(replay: dict, contracts: dict[str, dict]) -> dict:
    """Keep cached simulator probabilities while refreshing cheap baselines.

    Replaying a WC fixture is the expensive part: it starts the simulator bridge
    and prices every supported contract. Empirical baseline metadata is cheap
    and may change when the compact evidence artifact is rebuilt, so refresh it
    in-place instead of invalidating otherwise usable replay files.
    """
    fixture_stage = replay.get("stage")
    for row in replay.get("rows") or []:
        if fixture_stage and not row.get("stage"):
            row["stage"] = fixture_stage
        baseline = contracts.get(str(row.get("contract_key"))) or {}
        row["p_empirical"] = baseline.get("p_empirical")
        row["empirical_training_observations"] = baseline.get(
            "empirical_training_observations"
        )
    return replay


def _load_replay(
    fixture_id: int,
    catalog_hash: str,
    contracts: dict[str, dict],
) -> tuple[dict | None, bool]:
    try:
        replay = json.loads(_replay_path(fixture_id).read_text())
    except (OSError, json.JSONDecodeError):
        return None, False
    if replay.get("replay_version") not in {2, REPLAY_VERSION}:
        return None, False
    if replay.get("catalog_hash") != catalog_hash:
        return None, False
    dirty = (
        replay.get("replay_version") != REPLAY_VERSION
    )
    _refresh_replay_baselines(replay, contracts)
    if dirty:
        replay["replay_version"] = REPLAY_VERSION
        replay["catalog_hash"] = catalog_hash
        replay["baseline_refreshed_at"] = _now()
    return replay, dirty


def _write_replay(fixture_id: int, replay: dict) -> None:
    REPLAY_DIR.mkdir(parents=True, exist_ok=True)
    path = _replay_path(fixture_id)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(replay, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    )
    temporary.replace(path)


def _replay_fixture(
    covered: dict,
    contracts: dict[str, dict],
    catalog_hash: str,
) -> dict:
    fixture = covered["fixture"]
    fixture_id = int(covered["fixture_id"])
    home = _team_name(fixture, "home")
    away = _team_name(fixture, "away")
    if not home or not away:
        return {
            "replay_version": REPLAY_VERSION,
            "catalog_hash": catalog_hash,
            "fixture_id": fixture_id,
            "rows": [],
            "error": "missing team names",
        }

    markets = []
    metadata = {}
    for key, baseline in contracts.items():
        if not _contract_relevant_for_stage(key, covered["stage"]):
            continue
        labels = wc2026_evidence.labels_for_contract(key, covered["facts"])
        questions = questions_for_contract(key, home, away)
        if labels is None or len(labels) != len(questions):
            continue
        for index, (question, outcome) in enumerate(zip(questions, labels, strict=True)):
            market_id = f"c{len(markets)}"
            markets.append({"id": market_id, "question": question})
            metadata[market_id] = {
                "contract_key": key,
                "observation_index": index,
                "outcome": int(bool(outcome)),
                **baseline,
            }

    estimates = simulator.simulator_estimates(
        markets,
        PriceCtx(home, away, [], None, None),
        direct_by_market={market["id"]: [] for market in markets},
        kickoff=covered["kickoff"],
        stage=covered["stage"],
    )
    rows = []
    mismatches = []
    for market in markets:
        market_id = market["id"]
        estimate = estimates.get(market_id)
        meta = metadata[market_id]
        if not estimate:
            continue
        if estimate.get("contract_key") != meta["contract_key"]:
            mismatches.append({
                "expected": meta["contract_key"],
                "actual": estimate.get("contract_key"),
                "question": market["question"],
            })
            continue
        rows.append({
            "fixture_id": fixture_id,
            "kickoff": covered["kickoff"],
            "stage": covered["stage"],
            "family": str(estimate.get("family") or "unknown"),
            "contract_key": meta["contract_key"],
            "observation_index": meta["observation_index"],
            "observation_unit": observation_unit(meta["contract_key"]),
            "p_model": float(estimate["probability"]),
            "p_empirical": meta["p_empirical"],
            "empirical_training_observations": meta[
                "empirical_training_observations"
            ],
            "outcome": meta["outcome"],
        })
    return {
        "replay_version": REPLAY_VERSION,
        "catalog_hash": catalog_hash,
        "generated_at": _now(),
        "fixture_id": fixture_id,
        "match": f"{home} vs {away}",
        "kickoff": covered["kickoff"],
        "stage": covered["stage"],
        "requested_observations": len(markets),
        "rows": rows,
        "mismatches": mismatches,
    }


def _contract_relevant_for_stage(key: str, stage: str) -> bool:
    """Avoid asking match/advance contracts where group-stage scope aliases to regulation."""
    if stage == "knockout":
        return True
    if key == "match_result:team:advance":
        return False
    return ":match" not in key


def _clustered_ci(rows: list[dict]) -> list[float] | None:
    clusters: dict[int, list[float]] = defaultdict(list)
    for row in rows:
        clusters[int(row["fixture_id"])].append(
            (row["p_model"] - row["outcome"]) ** 2
            - (row["p_empirical"] - row["outcome"]) ** 2
        )
    count = sum(len(values) for values in clusters.values())
    if count < 2 or len(clusters) < 2:
        return None
    mean = sum(sum(values) for values in clusters.values()) / count
    sums = [sum(value - mean for value in values) for values in clusters.values()]
    variance = (
        len(clusters) / (len(clusters) - 1)
        * sum(value * value for value in sums)
        / (count * count)
    )
    margin = 1.96 * math.sqrt(max(variance, 0.0))
    return [round(mean - margin, 6), round(mean + margin, 6)]


def _sample_size(matches: int) -> dict:
    if matches < 30:
        level = "too_small"
        guidance = "Treat as inconclusive; do not choose a baseline from it."
    elif matches < 75:
        level = "limited"
        guidance = "Use only as a weak directional check."
    elif matches < 200:
        level = "moderate"
        guidance = "Use as supporting evidence with match-specific checks."
    else:
        level = "large"
        guidance = "Use as a meaningful reliability signal."
    return {
        "level": level,
        "basis": "unique settled labelable WC2026 fixtures",
        "guidance": guidance,
    }


def _summary(rows: list[dict], *, scope: str, contracts: int) -> dict:
    if not rows:
        return {
            "available": False,
            "reason": "No labelable settled WC2026 observations for this scope.",
        }
    all_matches = len({row["fixture_id"] for row in rows})
    comparable = [row for row in rows if row.get("p_empirical") is not None]
    scored = comparable or rows
    observations = len(scored)
    matches = len({row["fixture_id"] for row in scored})
    model_brier = sum(
        (row["p_model"] - row["outcome"]) ** 2 for row in scored
    ) / observations
    summary = {
        "available": True,
        "scope": scope,
        "evaluation": (
            "Frozen pre-2026 simulator applied to every labelable settled WC2026 "
            "fixture, independent of SportPredict question publication."
        ),
        "matches": matches,
        "observations": observations,
        "contracts": contracts,
        "coverage": {
            "labelable_matches": all_matches,
            "simulator_observations": len(rows),
            "comparable_matches": len({
                row["fixture_id"] for row in comparable
            }),
            "comparable_observations": len(comparable),
        },
        "brier": {
            "simulator": round(model_brier, 6),
            "always_50": 0.25,
        },
        "sample_size": _sample_size(matches),
    }
    if not comparable:
        summary["comparison_signal"] = "empirical_baseline_unavailable"
        return summary

    empirical_brier = sum(
        (row["p_empirical"] - row["outcome"]) ** 2 for row in comparable
    ) / len(comparable)
    shrunk_probability = _shrunk_empirical_probability(
        comparable, scope=scope, contracts=contracts,
    )
    ci = _clustered_ci(comparable)
    if summary["sample_size"]["level"] == "too_small":
        signal = "inconclusive_small_sample"
    elif ci and ci[1] < 0:
        signal = "simulator_better"
    elif ci and ci[0] > 0:
        signal = "empirical_rate_better"
    else:
        signal = "inconclusive"
    summary["brier"]["empirical_rate"] = round(empirical_brier, 6)
    if shrunk_probability is not None:
        shrunk_brier = sum(
            (shrunk_probability - row["outcome"]) ** 2
            for row in comparable
        ) / len(comparable)
        summary["brier"][SHRUNK_EMPIRICAL_RATE_SOURCE] = round(shrunk_brier, 6)
        summary["shrunk_empirical_rate"] = {
            "probability": round(shrunk_probability, 6),
            "prior": "all_history_empirical_rate",
            "historical_effective_observations": (
                LATE_HYDRATION_GOAL_EFFECTIVE_HISTORY
            ),
            "observed_yes_events": int(sum(row["outcome"] for row in comparable)),
            "observed_observations": len(comparable),
        }
    summary["delta_brier"] = {
        "simulator_minus_always_50": round(model_brier - 0.25, 6),
        "simulator_minus_empirical_rate": round(model_brier - empirical_brier, 6),
        "negative_favors_simulator": True,
    }
    summary["simulator_minus_empirical_rate_95pct_ci"] = ci
    summary["comparison_signal"] = signal
    return summary


def _shrunk_empirical_probability(
    rows: list[dict],
    *,
    scope: str,
    contracts: int,
) -> float | None:
    if scope != "wc2026_exhaustive_exact_contract" or contracts != 1:
        return None
    if not rows:
        return None
    keys = {str(row.get("contract_key")) for row in rows}
    if keys != {LATE_HYDRATION_GOAL_REG_CONTRACT}:
        return None
    priors = [
        float(row["p_empirical"]) for row in rows
        if row.get("p_empirical") is not None
    ]
    if len(priors) != len(rows):
        return None
    prior_rate = sum(priors) / len(priors)
    observations = len(rows)
    yes_events = sum(float(row["outcome"]) for row in rows)
    return (
        prior_rate * LATE_HYDRATION_GOAL_EFFECTIVE_HISTORY + yes_events
    ) / (LATE_HYDRATION_GOAL_EFFECTIVE_HISTORY + observations)


def _summaries(rows: list[dict], group_key: str, scope: str) -> dict[str, dict]:
    grouped: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        grouped[str(row[group_key])].append(row)
    result = {}
    for key, grouped_rows in sorted(grouped.items()):
        contracts = len({row["contract_key"] for row in grouped_rows})
        result[key] = {
            **_summary(grouped_rows, scope=scope, contracts=contracts),
            group_key: key,
        }
        if group_key == "contract_key":
            result[key]["observation_unit"] = observation_unit(key)
    return result


def _contract_summaries(rows: list[dict]) -> dict[str, dict]:
    grouped: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        grouped[str(row["contract_key"])].append(row)
    result = {}
    for key, contract_rows in sorted(grouped.items()):
        scopes = {
            "wc2026": _summary(
                contract_rows, scope="wc2026_exhaustive_exact_contract", contracts=1,
            ),
            "wc2026_knockout": _summary(
                [row for row in contract_rows if row.get("stage") == "knockout"],
                scope="wc2026_knockout_exhaustive_exact_contract",
                contracts=1,
            ),
        }
        for summary in scopes.values():
            if summary.get("available"):
                summary["contract_key"] = key
                summary["observation_unit"] = observation_unit(key)
        result[key] = scopes
    return result


def refresh(af, target_kickoff: str | None = None, *, path: Path = SNAPSHOT_PATH) -> dict:
    """Replay new final fixtures and atomically rebuild exhaustive comparisons."""
    artifact = _artifact()
    contracts = _benchmark_contracts(artifact)
    catalog_hash = _catalog_hash(contracts)
    target_kickoff = target_kickoff or _now()
    cutoff, eligible, covered, failures = wc2026_evidence.collect_fixture_facts(
        af, target_kickoff, set(contracts),
    )
    rows = []
    newly_replayed = 0
    replay_failures = []
    for fixture in covered:
        fixture_id = int(fixture["fixture_id"])
        replay, replay_dirty = _load_replay(fixture_id, catalog_hash, contracts)
        if replay is None:
            replay = _replay_fixture(fixture, contracts, catalog_hash)
            _write_replay(fixture_id, replay)
            newly_replayed += 1
        elif replay_dirty:
            _write_replay(fixture_id, replay)
        rows.extend(replay.get("rows") or [])
        if replay.get("error") or replay.get("mismatches"):
            replay_failures.append({
                "fixture_id": fixture_id,
                "error": replay.get("error"),
                "mismatches": replay.get("mismatches") or [],
            })

    snapshot = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": _now(),
        "target_kickoff": cutoff.isoformat(),
        "evaluation": "exhaustive_frozen_pre2026_simulator_on_wc2026",
        "population": "all_labelable_settled_fixtures",
        "eligible_matches": len(eligible),
        "replayed_matches": len({row["fixture_id"] for row in rows}),
        "simulator_observations": len(rows),
        "comparable_simulator_observations": sum(
            row.get("p_empirical") is not None for row in rows
        ),
        "contracts_evaluated": len({row["contract_key"] for row in rows}),
        "new_matches_replayed": newly_replayed,
        "complete": not failures and not replay_failures,
        "failures": failures + replay_failures,
        "families": _summaries(
            rows, "family", "wc2026_exhaustive_family_contracts",
        ),
        "contracts": _contract_summaries(rows),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(snapshot, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    )
    temporary.replace(path)
    return snapshot


def load(path: Path = SNAPSHOT_PATH) -> dict:
    try:
        snapshot = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return snapshot if snapshot.get("schema_version") == SCHEMA_VERSION else {}


def overlay(estimates: dict[str, dict], snapshot: dict) -> dict[str, dict]:
    """Attach exact-contract exhaustive WC2026 performance."""
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
        contract_performance = history.setdefault("contract_performance", {})
        if key in contracts:
            for scope, row in (contracts.get(key) or {}).items():
                if row.get("available"):
                    contract_performance[scope] = copy.deepcopy(row)
        estimate["historical_evidence"] = history
    return estimates
