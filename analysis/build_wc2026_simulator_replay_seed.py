"""Build the compact deployed WC2026 simulator-replay seed.

The tracked input replay was produced with model artifacts fitted only through
2025. The deployed settlement job appends newly settled matches to this
immutable seed, so tournament family comparisons remain current without
retraining.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

try:
    from analysis.build_simulator_family_benchmarks import (
        DEFAULT_SOURCE_ROOT,
        _read_csv,
        _resolve_csv,
        family_from_contract,
    )
except ModuleNotFoundError:  # Direct execution: python analysis/build_*.py
    from build_simulator_family_benchmarks import (  # type: ignore[no-redef]
        DEFAULT_SOURCE_ROOT,
        _read_csv,
        _resolve_csv,
        family_from_contract,
    )


def build(replay_path: Path, artifact_path: Path, catalog_path: Path | None = None) -> dict:
    artifact = json.loads(artifact_path.read_text())
    contracts = artifact.get("contracts") or {}
    kickoffs = {}
    if catalog_path and _resolve_csv(catalog_path).is_file():
        kickoffs = {
            row["match_id"]: row.get("kickoff")
            for row in _read_csv(catalog_path)
        }
    rows = []
    for row in _read_csv(replay_path):
        key = row["contract_key"]
        empirical = (
            ((contracts.get(key) or {}).get("empirical_rate") or {})
            .get("all_history") or {}
        )
        rows.append({
            "match_id": row["match_id"],
            "kickoff": kickoffs.get(row["match_id"]),
            "family": family_from_contract(key),
            "contract_key": key,
            "p_model": round(float(row["p_model"]), 8),
            "p_empirical": (
                round(float(empirical["rate"]), 8)
                if empirical.get("available") and empirical.get("rate") is not None
                else None
            ),
            "outcome": int(row["outcome"]),
        })
    return {
        "schema_version": 1,
        "source": "frozen pre-2026 simulator replay on settled WC2026 questions",
        "matches": len({row["match_id"] for row in rows}),
        "rows": rows,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--replay",
        type=Path,
        default=DEFAULT_SOURCE_ROOT / "notebooks" / "wc2026_simulator_oos_rows.csv",
    )
    parser.add_argument(
        "--artifact",
        type=Path,
        default=Path("simulator/data/processed/simulation_evidence.json"),
    )
    parser.add_argument(
        "--catalog",
        type=Path,
        default=(
            DEFAULT_SOURCE_ROOT
            / "data"
            / "processed"
            / "sportspredict_question_catalog.csv"
        ),
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=Path(
            "simulator/data/processed/wc2026_simulator_replay_seed.json"
        ),
    )
    args = parser.parse_args()
    seed = build(args.replay, args.artifact, args.catalog)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(seed, ensure_ascii=False, separators=(",", ":")) + "\n")
    print(f"[wc2026-replay-seed] matches={seed['matches']} rows={len(seed['rows'])} -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
