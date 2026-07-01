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


class TournamentFamilyBenchmarkTests(unittest.TestCase):
    def test_refresh_scores_exact_contract_for_both_teams_on_every_fixture(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifact = root / "artifact.json"
            key = "count:shots_on_target:team:full:>=:6:reg"
            artifact.write_text(json.dumps({"contracts": {key: {
                "empirical_rate": {"all_history": {
                    "available": True, "rate": 0.25, "observations": 8000,
                }},
            }}}))
            fixture = {
                "fixture": {"id": 1},
                "teams": {
                    "home": {"name": "Home"},
                    "away": {"name": "Away"},
                },
            }
            covered = {
                "fixture": fixture, "fixture_id": 1,
                "kickoff": "2026-06-30T12:00:00Z",
                "stage": "group", "facts": {},
            }

            def estimates(markets, *_args, **_kwargs):
                return {
                    market["id"]: {
                        "family": "count_threshold",
                        "contract_key": key,
                        "probability": probability,
                    }
                    for market, probability in zip(markets, (0.8, 0.2), strict=True)
                }

            with patch.object(simulator_benchmark, "ARTIFACT_PATH", artifact), patch.object(
                simulator_benchmark, "REPLAY_DIR", root / "replays"
            ), patch.object(
                simulator_benchmark.wc2026_evidence, "collect_fixture_facts",
                return_value=(
                    simulator_benchmark.datetime(2026, 7, 1, tzinfo=simulator_benchmark.timezone.utc),
                    [fixture], [covered], [],
                ),
            ), patch.object(
                simulator_benchmark.wc2026_evidence, "labels_for_contract",
                return_value=[True, False],
            ), patch.object(
                simulator_benchmark.simulator, "simulator_estimates",
                side_effect=estimates,
            ):
                snapshot = simulator_benchmark.refresh(
                    object(), path=root / "snapshot.json",
                )

        family = snapshot["families"]["count_threshold"]
        contract = snapshot["contracts"][key]["wc2026"]
        self.assertEqual(contract["observations"], 2)
        self.assertEqual(contract["matches"], 1)
        self.assertEqual(contract["observation_unit"], "team")
        self.assertAlmostEqual(contract["brier"]["simulator"], 0.04)
        self.assertAlmostEqual(contract["brier"]["empirical_rate"], 0.3125)
        self.assertEqual(family["comparison_signal"], "inconclusive_small_sample")
        self.assertEqual(family["sample_size"]["level"], "too_small")
        self.assertEqual(snapshot["replayed_matches"], 1)

        estimates = {"q1": {
            "family": "count_threshold", "contract_key": key,
            "historical_evidence": {"family_performance": {
                "live_wc2026": {"available": True},
            }},
        }}
        simulator_benchmark.overlay(estimates, snapshot)
        performance = estimates["q1"]["historical_evidence"]["family_performance"]
        self.assertNotIn("live_wc2026", performance)
        self.assertEqual(performance["wc2026"]["observations"], 2)
        exact = estimates["q1"]["historical_evidence"]["contract_performance"]["wc2026"]
        self.assertEqual(exact["observations"], 2)

    def test_family_without_empirical_baseline_still_reports_simulator_vs_50(self):
        summary = simulator_benchmark._summary([{
            "fixture_id": 1, "family": "total_goals",
            "contract_key": "player_stat:test", "p_model": 0.8,
            "p_empirical": None, "outcome": 1,
        }], scope="test", contracts=1)
        self.assertEqual(
            summary["comparison_signal"], "empirical_baseline_unavailable",
        )
        self.assertAlmostEqual(summary["brier"]["simulator"], 0.04)
        self.assertEqual(summary["brier"]["always_50"], 0.25)
        self.assertNotIn("empirical_rate", summary["brier"])


if __name__ == "__main__":
    unittest.main()
