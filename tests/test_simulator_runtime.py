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
        self.assertEqual(report["schema_version"], "2.0")
        self.assertEqual(report["model"]["rate_model"], "LearnedRateModel")
        by_id = {item["market_id"]: item for item in report["question_reports"]}
        self.assertEqual(set(by_id), {"penalty", "brace"})
        self.assertEqual(by_id["penalty"]["contract_key"], "penalty_awarded:match")
        brace_history = by_id["brace"]["historical_evidence"]["empirical_rate"]
        self.assertTrue(brace_history["all_history"]["available"])

    def test_late_goal_template_includes_extra_time_by_default(self):
        payload = {
            "home": "Netherlands", "away": "Morocco",
            "kickoff": "2026-06-30T01:00:00Z", "stage": "knockout",
            "questions": [{
                "market_id": "late",
                "question": "Will a goal be scored after the second hydration break?",
            }],
            "market_odds": {"regulation_draw_probability": 0.306321},
            "n_sims": 100,
        }
        report = self._run_bridge(payload)
        item = report["question_reports"][0]
        self.assertEqual(item["contract_key"], "goal_window:after_second_hydration:et")
        self.assertIn("including extra time", item["explanation"])
        self.assertEqual(
            item["conditioning_inputs"]["regulation_draw_probability"], 0.306321,
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
