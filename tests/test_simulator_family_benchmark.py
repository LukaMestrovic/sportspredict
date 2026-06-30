import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from analysis.build_simulator_family_benchmarks import (
    family_from_contract,
    family_performance,
)
from bot import simulator_benchmark


class StaticFamilyBenchmarkTests(unittest.TestCase):
    def test_contracts_roll_up_to_report_families(self):
        self.assertEqual(
            family_from_contract("count:goals:team:full:>=:1:reg"),
            "count_threshold",
        )
        self.assertEqual(
            family_from_contract("compare:corners:2H:reg"),
            "team_vs_team_more",
        )

    def test_family_comparison_scores_all_baselines_on_identical_unseen_rows(self):
        rows = [
            {
                "family": "goal_window", "contract_key": "goal_window:test",
                "match_id": f"m{index}", "fold_year": 2026,
                "match_date": f"2026-06-{index + 1:02d}", "outcome": float(index % 2),
                "p_model": float(index % 2), "p_empirical": 0.5,
                "empirical_training_observations": 100,
            }
            for index in range(30)
        ]
        result = family_performance(rows, scope="test")["goal_window"]
        self.assertEqual(result["questions"], 30)
        self.assertEqual(result["matches"], 30)
        self.assertEqual(result["brier"]["simulator"], 0.0)
        self.assertEqual(result["brier"]["always_50"], 0.25)
        self.assertEqual(result["brier"]["empirical_rate"], 0.25)
        self.assertEqual(result["comparison_signal"], "simulator_better")
        self.assertEqual(result["sample_size"]["level"], "limited")

    def test_shipped_artifact_has_separate_wc_sample_warning(self):
        path = Path("simulator/data/processed/simulation_evidence.json")
        artifact = json.loads(path.read_text())
        benchmark = artifact["families"]["goal_window"]
        self.assertEqual(artifact["schema_version"], 2)
        self.assertEqual(benchmark["all_history"]["sample_size"]["level"], "large")
        self.assertEqual(benchmark["wc2026"]["matches"], 34)
        self.assertEqual(benchmark["wc2026"]["sample_size"]["level"], "limited")


class LiveFamilyBenchmarkTests(unittest.TestCase):
    def test_refresh_uses_frozen_simulator_and_empirical_predictions(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            evidence = root / "evidence.json"
            evidence.write_text(json.dumps({
                "question_evidence": [{
                    "market_id": "q1",
                    "simulator_model_estimates": [{
                        "family": "goal_window",
                        "contract_key": "goal_window:test",
                        "probability": 0.8,
                        "historical_evidence": {"empirical_rate": {
                            "all_history": {"available": True, "rate": 0.6},
                        }},
                    }],
                }],
            }))
            ledger = root / "ledger.sqlite3"
            db = sqlite3.connect(ledger)
            db.executescript("""
                CREATE TABLE runs (
                    id TEXT, match_id TEXT, recorded_at TEXT,
                    evidence_path TEXT, status TEXT
                );
                CREATE TABLE questions (
                    run_id TEXT, market_id TEXT, outcome INTEGER
                );
            """)
            db.execute(
                "INSERT INTO runs VALUES (?, ?, ?, ?, ?)",
                ("r1", "m1", "2026-06-30T12:00:00Z", str(evidence), "submitted"),
            )
            db.execute("INSERT INTO questions VALUES (?, ?, ?)", ("r1", "q1", 1))
            db.commit()
            db.close()

            snapshot = simulator_benchmark.refresh(ledger, path=root / "snapshot.json")

        family = snapshot["families"]["goal_window"]
        self.assertEqual(family["questions"], 1)
        self.assertAlmostEqual(family["brier"]["simulator"], 0.04)
        self.assertAlmostEqual(family["brier"]["empirical_rate"], 0.16)
        self.assertEqual(family["comparison_signal"], "inconclusive_small_sample")
        self.assertEqual(family["sample_size"]["level"], "too_small")

        estimates = {"q1": {"family": "goal_window", "historical_evidence": {}}}
        simulator_benchmark.overlay(estimates, snapshot)
        live = estimates["q1"]["historical_evidence"]["family_performance"]["live_wc2026"]
        self.assertEqual(live["questions"], 1)


if __name__ == "__main__":
    unittest.main()
