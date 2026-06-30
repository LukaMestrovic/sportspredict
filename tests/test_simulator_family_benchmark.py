import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

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
        self.assertEqual(benchmark["wc2026"]["matches"], 1)
        self.assertEqual(benchmark["wc2026"]["sample_size"]["level"], "too_small")
        seed = json.loads(Path(
            "simulator/data/processed/wc2026_simulator_replay_seed.json"
        ).read_text())
        self.assertEqual(seed["matches"], 73)


class TournamentFamilyBenchmarkTests(unittest.TestCase):
    def test_refresh_uses_frozen_simulator_and_empirical_predictions(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            seed = root / "seed.json"
            seed.write_text(json.dumps({"rows": [{
                "match_id": "m1", "kickoff": "2026-06-30T12:00:00Z",
                "family": "goal_window", "contract_key": "goal_window:test",
                "p_model": 0.8, "p_empirical": 0.6, "outcome": 1,
            }]}))

            class _Web:
                def settled_matches(self, event_id, refresh=False):
                    return [{"id": "m1"}]

            with patch.object(simulator_benchmark, "SEED_PATH", seed), patch.object(
                simulator_benchmark, "REPLAY_DIR", root / "replays"
            ):
                snapshot = simulator_benchmark.refresh(
                    None, _Web(), "event", "lobby", path=root / "snapshot.json",
                )

        family = snapshot["families"]["goal_window"]
        self.assertEqual(family["questions"], 1)
        self.assertAlmostEqual(family["brier"]["simulator"], 0.04)
        self.assertAlmostEqual(family["brier"]["empirical_rate"], 0.16)
        self.assertEqual(family["comparison_signal"], "inconclusive_small_sample")
        self.assertEqual(family["sample_size"]["level"], "too_small")
        self.assertEqual(snapshot["replayed_matches"], 1)

        estimates = {"q1": {
            "family": "goal_window", "contract_key": "goal_window:test",
            "historical_evidence": {"family_performance": {
                "live_wc2026": {"available": True},
            }},
        }}
        simulator_benchmark.overlay(estimates, snapshot)
        performance = estimates["q1"]["historical_evidence"]["family_performance"]
        self.assertNotIn("live_wc2026", performance)
        self.assertEqual(performance["wc2026"]["questions"], 1)
        rate = estimates["q1"]["historical_evidence"]["empirical_rate"]["wc2026"]
        self.assertEqual(rate["population"], "settled_question_instances")

    def test_family_without_empirical_baseline_still_reports_simulator_vs_50(self):
        summary = simulator_benchmark._summaries([{
            "match_id": "m1", "family": "player_stat",
            "contract_key": "player_stat:test", "p_model": 0.8,
            "p_empirical": None, "outcome": 1,
        }])["player_stat"]
        self.assertEqual(
            summary["comparison_signal"], "empirical_baseline_unavailable",
        )
        self.assertAlmostEqual(summary["brier"]["simulator"], 0.04)
        self.assertEqual(summary["brier"]["always_50"], 0.25)
        self.assertNotIn("empirical_rate", summary["brier"])


if __name__ == "__main__":
    unittest.main()
