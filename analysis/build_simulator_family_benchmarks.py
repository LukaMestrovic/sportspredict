"""Add leakage-safe family benchmarks to the shipped simulator evidence.

The production runtime deliberately does not contain training data or tooling.
This analysis helper reads compact rolling-origin exports tracked in this
repository and enriches the compact artifact that is tracked here.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import json
import math
from collections import defaultdict
from pathlib import Path


DEFAULT_SOURCE_ROOT = Path(__file__).resolve().parent / "data" / "simulator_benchmarks"
LATE_GOAL_ET = "goal_window:after_second_hydration:et"
LATE_GOAL_REG = "goal_window:after_second_hydration:reg"
KEY_ALIASES = {
    "substitution_before_halftime:match": "substitution_before_halftime:reg",
}
FAMILY_ALIASES = {
    "compare": "team_vs_team_more",
    "compound": "compound_and",
    "count": "count_threshold",
}


def family_from_contract(contract_key: str) -> str:
    """Return the simulator report family represented by an exact contract."""
    prefix = contract_key.split(":", 1)[0]
    return FAMILY_ALIASES.get(prefix, prefix)


def _canonical_key(key: str) -> str:
    return KEY_ALIASES.get(key, key)


def _resolve_csv(path: Path) -> Path:
    if path.is_file():
        return path
    compressed = path.with_name(path.name + ".gz")
    if compressed.is_file():
        return compressed
    return path


def _csv_paths(root: Path, pattern: str) -> list[Path]:
    return sorted({
        *root.glob(pattern),
        *root.glob(f"{pattern}.gz"),
    })


def _read_csv(path: Path) -> list[dict[str, str]]:
    resolved = _resolve_csv(path)
    if resolved.suffix == ".gz":
        with gzip.open(resolved, "rt", newline="") as handle:
            return list(csv.DictReader(handle))
    with resolved.open(newline="") as handle:
        return list(csv.DictReader(handle))


def _fold_rates(source_root: Path, years: set[int]) -> dict[int, dict[str, dict]]:
    result: dict[int, dict[str, dict]] = {}
    for year in sorted(years):
        path = source_root / "exports" / f"oos_{year}" / "exotic_empirical_rates.csv"
        rates = {}
        for row in _read_csv(path):
            rates[_canonical_key(row["contract_key"])] = {
                "rate": float(row["empirical_rate"]),
                "observations": int(float(row["n_all"])),
            }
        result[year] = rates
    return result


def load_comparison_rows(source_root: Path, contract_keys: set[str]) -> list[dict]:
    """Load OOS predictions and attach empirical rates learned before each fold."""
    paths = _csv_paths(source_root / "exports", "oos_*/exotic_oos_rows.csv")
    raw_rows = [row for path in paths for row in _read_csv(path)]
    years = {int(row["fold_year"]) for row in raw_rows}
    rates = _fold_rates(source_root, years)
    prepared = []
    for row in raw_rows:
        key = _canonical_key(row["contract_key"])
        # In group matches, an include-ET price and a regulation price are the
        # same observable contract because extra time cannot occur.
        if key == LATE_GOAL_ET and row.get("stage") != "knockout":
            key = LATE_GOAL_REG
        if key not in contract_keys:
            continue
        year = int(row["fold_year"])
        baseline = rates[year].get(key)
        if baseline is None and key == LATE_GOAL_REG:
            baseline = rates[year].get(LATE_GOAL_ET)
        prepared.append({
            "family": family_from_contract(key),
            "contract_key": key,
            "match_id": row["match_id"],
            "stage": row.get("stage"),
            "fold_year": year,
            "tournament": row.get("tournament"),
            "match_date": row.get("match_date"),
            "outcome": float(row["outcome"]),
            "p_model": float(row["p_model"]),
            "p_empirical": baseline["rate"] if baseline else None,
            "empirical_training_observations": baseline["observations"] if baseline else None,
        })
    return prepared


def _clustered_ci(differences: list[tuple[str, float]]) -> list[float] | None:
    """95% cluster-robust CI for mean paired Brier difference, by match."""
    clusters: dict[str, list[float]] = defaultdict(list)
    for match_id, value in differences:
        clusters[match_id].append(value)
    count = len(differences)
    n_clusters = len(clusters)
    if count < 2 or n_clusters < 2:
        return None
    mean = sum(value for _, value in differences) / count
    cluster_sums = [sum(value - mean for value in values) for values in clusters.values()]
    variance = (n_clusters / (n_clusters - 1)) * sum(x * x for x in cluster_sums) / (count * count)
    margin = 1.96 * math.sqrt(max(variance, 0.0))
    return [round(mean - margin, 6), round(mean + margin, 6)]


def _sample_assessment(matches: int) -> dict[str, str]:
    if matches < 30:
        return {
            "level": "too_small",
            "guidance": "Treat the family comparison as inconclusive; do not choose a signal from it.",
        }
    if matches < 75:
        return {
            "level": "limited",
            "guidance": "Use only as a weak directional check; the tournament sample is still small.",
        }
    if matches < 200:
        return {
            "level": "moderate",
            "guidance": "Use as supporting evidence, while retaining contract and match-specific checks.",
        }
    return {
        "level": "large",
        "guidance": "The sample is broad enough to use as a meaningful family-level reliability signal.",
    }


def family_performance(rows: list[dict], *, scope: str) -> dict[str, dict]:
    """Score simulator, 50/50, and prior empirical-rate rules on identical rows."""
    grouped: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        grouped[row["family"]].append(row)
    result = {}
    for family, all_rows in sorted(grouped.items()):
        comparable = [row for row in all_rows if row["p_empirical"] is not None]
        if not comparable:
            continue
        model_losses = [(row["p_model"] - row["outcome"]) ** 2 for row in comparable]
        empirical_losses = [(row["p_empirical"] - row["outcome"]) ** 2 for row in comparable]
        model_brier = sum(model_losses) / len(model_losses)
        empirical_brier = sum(empirical_losses) / len(empirical_losses)
        delta_empirical = model_brier - empirical_brier
        differences = [
            (row["match_id"], model_loss - empirical_loss)
            for row, model_loss, empirical_loss in zip(
                comparable, model_losses, empirical_losses, strict=True,
            )
        ]
        matches = len({row["match_id"] for row in comparable})
        assessment = _sample_assessment(matches)
        ci = _clustered_ci(differences)
        if assessment["level"] == "too_small":
            signal = "inconclusive_small_sample"
        elif ci and ci[1] < 0:
            signal = "simulator_better"
        elif ci and ci[0] > 0:
            signal = "empirical_rate_better"
        else:
            signal = "inconclusive"
        training_sizes = [row["empirical_training_observations"] for row in comparable]
        baseline_rates = [row["p_empirical"] for row in comparable]
        result[family] = {
            "available": True,
            "scope": scope,
            "family": family,
            "evaluation": (
                (
                    "Frozen pre-2026 simulator predictions are scored on settled WC2026 "
                    "questions. The empirical baseline uses the exact contract's YES rate "
                    "estimated before the tournament."
                )
                if scope.startswith("wc2026_")
                else (
                    "Predictions are scored only on later, unseen matches. The empirical-rate "
                    "baseline uses the exact contract's YES rate estimated before each test fold."
                )
            ),
            "questions": len(comparable),
            "matches": matches,
            "contracts": len({row["contract_key"] for row in comparable}),
            "coverage": {
                "comparable_questions": len(comparable),
                "family_questions": len(all_rows),
                "fraction": round(len(comparable) / len(all_rows), 6),
            },
            "test_folds": sorted({row["fold_year"] for row in comparable}),
            "data_through": max(row["match_date"] or "" for row in comparable) or None,
            "brier": {
                "simulator": round(model_brier, 6),
                "always_50": 0.25,
                "empirical_rate": round(empirical_brier, 6),
            },
            "delta_brier": {
                "simulator_minus_always_50": round(model_brier - 0.25, 6),
                "simulator_minus_empirical_rate": round(delta_empirical, 6),
                "negative_favors_simulator": True,
            },
            "simulator_minus_empirical_rate_95pct_ci": ci,
            "comparison_signal": signal,
            "sample_size": {
                **assessment,
                "basis": "unique unseen matches, with uncertainty clustered by match",
            },
            "empirical_baseline": {
                "rule": "Always predict the prior-fold empirical YES rate for the exact contract.",
                "rate_range": [round(min(baseline_rates), 6), round(max(baseline_rates), 6)],
                "training_observations_range": [min(training_sizes), max(training_sizes)],
            },
        }
    return result


def _performance_summary(rows: list[dict], *, scope: str) -> dict:
    """Score simulator vs 50/50 and prior empirical-rate on identical rows."""
    comparable = [row for row in rows if row["p_empirical"] is not None]
    if not comparable:
        return {
            "available": False,
            "reason": "No comparable unseen rows with an empirical-rate baseline.",
        }
    model_losses = [(row["p_model"] - row["outcome"]) ** 2 for row in comparable]
    empirical_losses = [(row["p_empirical"] - row["outcome"]) ** 2 for row in comparable]
    model_brier = sum(model_losses) / len(model_losses)
    empirical_brier = sum(empirical_losses) / len(empirical_losses)
    differences = [
        (row["match_id"], model_loss - empirical_loss)
        for row, model_loss, empirical_loss in zip(
            comparable, model_losses, empirical_losses, strict=True,
        )
    ]
    ci = _clustered_ci(differences)
    matches = len({row["match_id"] for row in comparable})
    assessment = _sample_assessment(matches)
    if assessment["level"] == "too_small":
        signal = "inconclusive_small_sample"
    elif ci and ci[1] < 0:
        signal = "simulator_better"
    elif ci and ci[0] > 0:
        signal = "empirical_rate_better"
    else:
        signal = "inconclusive"
    return {
        "available": True,
        "scope": scope,
        "observations": len(comparable),
        "matches": matches,
        "brier": {
            "simulator": round(model_brier, 6),
            "empirical_rate": round(empirical_brier, 6),
            "always_50": 0.25,
        },
        "comparison_signal": signal,
        "sample_size": {
            **assessment,
            "basis": "unique unseen matches, with uncertainty clustered by match",
        },
        "simulator_minus_empirical_rate_95pct_ci": ci,
    }


def contract_performance(rows: list[dict], *, scope: str) -> dict[str, dict]:
    grouped: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        grouped[row["contract_key"]].append(row)
    return {
        key: _performance_summary(contract_rows, scope=scope)
        for key, contract_rows in sorted(grouped.items())
    }


def knockout_empirical_rates(rows: list[dict]) -> dict[str, dict]:
    grouped: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        if row.get("stage") == "knockout":
            grouped[row["contract_key"]].append(row)
    result = {}
    for key, contract_rows in sorted(grouped.items()):
        yes = sum(float(row["outcome"]) for row in contract_rows)
        result[key] = {
            "available": True,
            "rate": round(yes / len(contract_rows), 6),
            "observations": len(contract_rows),
            "matches": len({row["match_id"] for row in contract_rows}),
            "yes_events": int(yes),
            "population": "historical_knockout_labelable_observations",
        }
    return result


def build_family_benchmarks(source_root: Path, artifact: dict) -> dict[str, dict]:
    rows = load_comparison_rows(source_root, set(artifact.get("contracts", {})))
    all_history = family_performance(rows, scope="rolling_origin_all_history")
    prior_rates = {
        key: ((record.get("empirical_rate") or {}).get("all_history") or {})
        for key, record in (artifact.get("contracts") or {}).items()
    }
    wc2026_rows = []
    replay_path = source_root / "exports" / "wc2026_simulator_oos_rows.csv"
    if _resolve_csv(replay_path).is_file():
        for row in _read_csv(replay_path):
            key = _canonical_key(row["contract_key"])
            prior = prior_rates.get(key) or {}
            wc2026_rows.append({
                "family": family_from_contract(key),
                "contract_key": key,
                "match_id": row["match_id"],
                "fold_year": 2026,
                "tournament": "WORLDCUP2026",
                "match_date": None,
                "outcome": float(row["outcome"]),
                "p_model": float(row["p_model"]),
                "p_empirical": (
                    float(prior["rate"])
                    if prior.get("available") and prior.get("rate") is not None
                    else None
                ),
                "empirical_training_observations": prior.get("observations"),
            })
    wc2026 = family_performance(
        wc2026_rows,
        scope="wc2026_frozen_pre2026_simulator_replay",
    )
    return {
        family: {
            "family": family,
            "all_history": all_history.get(family, {
                "available": False, "reason": "No comparable unseen rows for this family.",
            }),
            "wc2026": wc2026.get(family, {
                "available": False, "reason": "No comparable unseen WC2026 rows for this family.",
            }),
        }
        for family in sorted(set(all_history) | set(wc2026))
    }


def enrich_contract_benchmarks(source_root: Path, artifact: dict) -> None:
    rows = load_comparison_rows(source_root, set(artifact.get("contracts", {})))
    all_contracts = contract_performance(rows, scope="rolling_origin_all_history")
    knockout_contracts = contract_performance(
        [row for row in rows if row.get("stage") == "knockout"],
        scope="rolling_origin_all_history_knockout",
    )
    knockout_rates = knockout_empirical_rates(rows)
    for key, record in (artifact.get("contracts") or {}).items():
        if key in knockout_rates:
            record.setdefault("empirical_rate", {})[
                "all_history_knockout"
            ] = knockout_rates[key]
        performance = record.setdefault("contract_performance", {})
        if key in all_contracts and all_contracts[key].get("available"):
            performance["all_history"] = all_contracts[key]
        if key in knockout_contracts and knockout_contracts[key].get("available"):
            performance["all_history_knockout"] = knockout_contracts[key]


def _main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, default=DEFAULT_SOURCE_ROOT)
    parser.add_argument(
        "--artifact", type=Path,
        default=Path("simulator/data/processed/simulation_evidence.json"),
    )
    args = parser.parse_args()
    artifact = json.loads(args.artifact.read_text())
    artifact["schema_version"] = 2
    artifact.setdefault("methodology", {})["family_comparison"] = (
        "Family-level Brier comparison on identical unseen rows. The simulator and 50/50 are "
        "compared with an exact-contract empirical-rate rule fitted only on data before each test "
        "fold; uncertainty is clustered by match. WC2026 replays settled tournament questions "
        "with simulator artifacts and empirical rates frozen before 2026."
    )
    artifact["families"] = build_family_benchmarks(args.source_root, artifact)
    enrich_contract_benchmarks(args.source_root, artifact)
    args.artifact.write_text(json.dumps(artifact, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    print(f"[family-benchmarks] families={len(artifact['families'])} -> {args.artifact}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
