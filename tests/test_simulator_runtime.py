"""Integration contract for the simulator bundled in this repository."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
SIMULATOR = ROOT / "simulator"


class BundledSimulatorTests(unittest.TestCase):
    def test_bridge_loads_local_model_and_audit_evidence(self):
        payload = {
            "home": "NED",
            "away": "MAR",
            "kickoff": "2026-06-30T00:00:00Z",
            "stage": "knockout",
            "n_sims": 100,
            "questions": [
                {
                    "market_id": "penalty",
                    "question": "Will a penalty kick be awarded in the match?",
                },
                {
                    "market_id": "brace",
                    "question": (
                        "Will any player score more than 1 goal "
                        "(excluding own goals) in the match?"
                    ),
                },
            ],
        }
        env = os.environ.copy()
        env["PYTHONPATH"] = str(SIMULATOR / "src")
        env["SPORTSPREDICT_ROOT"] = str(SIMULATOR)
        proc = subprocess.run(
            [sys.executable, "-m", "sphybrid.bridge"],
            cwd=SIMULATOR,
            env=env,
            input=json.dumps(payload),
            text=True,
            capture_output=True,
            timeout=60,
            check=False,
        )

        self.assertEqual(proc.returncode, 0, proc.stderr)
        report = json.loads(proc.stdout)
        self.assertEqual(report["schema_version"], "2.2")
        self.assertEqual(report["model"]["rate_model"], "LearnedRateModel")
        by_id = {item["market_id"]: item for item in report["question_reports"]}
        self.assertEqual(set(by_id), {"penalty", "brace"})
        self.assertEqual(by_id["penalty"]["contract_key"], "penalty_awarded:match")
        brace_history = by_id["brace"]["historical_evidence"]["empirical_rate"]
        self.assertTrue(brace_history["all_history"]["available"])
        family = by_id["brace"]["historical_evidence"]["family_performance"]
        self.assertEqual(family["family"], "any_player_threshold")
        self.assertTrue(family["all_history"]["available"])
        self.assertIn("empirical_rate", family["all_history"]["brier"])

    def test_any_player_two_or_more_goals_uses_historical_brace_key(self):
        payload = {
            "home": "Switzerland",
            "away": "Algeria",
            "kickoff": "2026-07-03T03:00:00Z",
            "stage": "knockout",
            "n_sims": 100,
            "questions": [{
                "market_id": "brace",
                "question": (
                    "Will any player score 2 or more goals in regulation "
                    "(90 minutes + stoppage time)?"
                ),
            }],
        }

        report = self._run_bridge(payload)
        item = report["question_reports"][0]
        self.assertEqual(item["contract_key"], "any_player_threshold:goals:>:1:reg")
        history = item["historical_evidence"]
        self.assertTrue(history["empirical_rate"]["all_history"]["available"])
        self.assertEqual(history["empirical_rate"]["all_history"]["observations"], 2974)
        comparison = history["contract_performance"]["all_history"]["brier"]
        self.assertIn("simulator", comparison)
        self.assertIn("empirical_rate", comparison)
        self.assertIn("always_50", comparison)

    def test_late_goal_template_includes_extra_time_by_default(self):
        payload = {
            "home": "Netherlands", "away": "Morocco",
            "kickoff": "2026-06-30T01:00:00Z", "stage": "knockout",
            "questions": [{
                "market_id": "late",
                "question": "Will a goal be scored after the second hydration break?",
            }],
            "n_sims": 100,
        }
        report = self._run_bridge(payload)
        item = report["question_reports"][0]
        self.assertEqual(item["contract_key"], "goal_window:after_second_hydration:et")
        self.assertIn("including extra time", item["explanation"])
        self.assertNotIn("conditioning_inputs", item)

    def test_first_goal_scope_distinguishes_regulation_from_full_match(self):
        payload = {
            "home": "France", "away": "Sweden",
            "kickoff": "2026-06-30T21:00:00Z", "stage": "knockout",
            "questions": [
                {
                    "market_id": "full",
                    "question": "Will France score the first goal of the match?",
                },
                {
                    "market_id": "reg",
                    "question": (
                        "Will France score the first goal in regulation "
                        "(90 minutes + stoppage time)?"
                    ),
                },
            ],
            "n_sims": 100,
        }
        report = self._run_bridge(payload)
        by_id = {item["market_id"]: item for item in report["question_reports"]}
        self.assertEqual(by_id["full"]["contract_key"], "first_goal:full:et:team")
        self.assertIn("including extra time", by_id["full"]["explanation"])
        self.assertEqual(by_id["reg"]["contract_key"], "first_goal:full:team")
        self.assertIn("regulation only", by_id["reg"]["explanation"])

    def test_team_score_excluding_own_goals_has_dedicated_counter(self):
        report = self._run_bridge({
            "home": "England", "away": "Congo DR",
            "kickoff": "2026-07-01T16:00:00Z", "stage": "knockout",
            "questions": [{
                "market_id": "team",
                "question": (
                    "Will DR Congo score a goal (excluding own goals) in regulation "
                    "(90 minutes + stoppage time)?"
                ),
            }],
            "n_sims": 100,
        })
        item = report["question_reports"][0]
        self.assertEqual(item["family"], "team_score_no_own")
        self.assertEqual(item["contract_key"], "team_score_no_own:reg")

    def test_bridge_canonicalizes_team_codes_for_learned_ratings(self):
        sys.path.insert(0, str(SIMULATOR / "src"))
        from sphybrid.report import context_from_payload

        ctx = context_from_payload({
            "home": "NED",
            "away": "MAR",
            "kickoff": "2026-06-30T01:00:00Z",
        })

        self.assertEqual(ctx.team_a, "Netherlands")
        self.assertEqual(ctx.team_b, "Morocco")
        self.assertIn("NED", ctx.extra["aliases"]["A"])
        self.assertIn("MAR", ctx.extra["aliases"]["B"])

    def test_ninety_minute_wording_routes_to_regulation_contracts(self):
        report = self._run_bridge({
            "home": "France",
            "away": "Sweden",
            "kickoff": "2026-06-30T21:00:00Z",
            "stage": "knockout",
            "n_sims": 100,
            "questions": [
                {
                    "market_id": "penalty",
                    "question": (
                        "Will a penalty kick be awarded "
                        "(90 minutes + stoppage time)?"
                    ),
                },
                {
                    "market_id": "card",
                    "question": (
                        "Will France receive at least 1 card "
                        "(90 minutes + stoppage time)?"
                    ),
                },
            ],
        })
        by_id = {item["market_id"]: item for item in report["question_reports"]}

        self.assertEqual(by_id["penalty"]["contract_key"], "penalty_awarded:reg")
        self.assertEqual(
            by_id["card"]["contract_key"],
            "count:cards:team:full:>=:1:reg",
        )

    def _run_bridge(self, payload):
        env = os.environ.copy()
        env["PYTHONPATH"] = str(SIMULATOR / "src")
        env["SPORTSPREDICT_ROOT"] = str(SIMULATOR)
        proc = subprocess.run(
            [sys.executable, "-m", "sphybrid.bridge"], cwd=SIMULATOR, env=env,
            input=json.dumps(payload), text=True, capture_output=True,
            timeout=60, check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return json.loads(proc.stdout)


if __name__ == "__main__":
    unittest.main()
