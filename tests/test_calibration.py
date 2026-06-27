import json
import unittest
from unittest.mock import patch

from bot import calibration


class FamilyTests(unittest.TestCase):
    def test_recurring_families_and_compound_overrides(self):
        cases = (
            ("Will Home win the match?", {"market": "match_winner"},
             "match_result_timing"),
            ("Will there be 3 or more total goals?", {"market": "total_goals"},
             "goals_team_scoring"),
            ("Will Away have 5 or more corner kicks?", None, "corners"),
            ("Will Home be caught offside 2 or more times?", None, "offsides"),
            ("Will Home commit more fouls than Away?", None, "fouls"),
            ("Will Player have 2 or more shots on target?",
             {"market": "player_shots_on_target"}, "player_shots_on_target"),
            ("Will Player score or assist a goal (excluding own goals)?", None,
             "player_goal_involvement"),
            ("Will both teams score AND the match have 3 or more total goals?",
             {"market": "none"}, "goal_compound"),
            ("Will a penalty kick be awarded OR a red card be shown?",
             {"market": "none"}, "penalty_red_card"),
            ("At halftime, will both teams have at least 1 shot on target?",
             {"market": "none"}, "both_teams_shots_on_target"),
        )
        for question, intent, expected in cases:
            with self.subTest(question=question):
                self.assertEqual(calibration.family_for(question, intent), expected)

    def test_family_does_not_depend_on_team_alias_parse(self):
        question = "Will Ivory Coast be caught offside 2 or more times?"
        self.assertEqual(calibration.family_for(question, None), "offsides")


class ModelTests(unittest.TestCase):
    def test_empty_fit_is_identity(self):
        model = calibration.fit_model([])
        self.assertEqual(model.probability_int(37, "unknown", "unknown"), 37)

    def test_mapping_is_monotone_and_bounded(self):
        model = calibration.fit_model(_biased_rows(30))
        values = [model.probability_int(p, "goals_team_scoring", "cohort-a")
                  for p in range(1, 100)]
        self.assertEqual(values, sorted(values))
        self.assertTrue(all(1 <= value <= 99 for value in values))
        self.assertGreaterEqual(model.slope, calibration.MIN_BETA)
        self.assertLessEqual(model.slope, calibration.MAX_BETA)

    def test_fit_and_hash_are_row_order_independent(self):
        rows = _biased_rows(12)
        first = calibration.fit_model(rows).to_dict()
        second = calibration.fit_model(list(reversed(rows))).to_dict()
        self.assertEqual(first, second)
        self.assertEqual(
            calibration.observation_hash(rows),
            calibration.observation_hash(list(reversed(rows))),
        )

    def test_snapshot_round_trip(self):
        snapshot = calibration.build_snapshot(
            _biased_rows(8), created_at="2026-06-27T00:00:00Z", bootstrap_samples=20,
        )
        restored = calibration.CalibrationSnapshot.from_dict(
            json.loads(json.dumps(snapshot.to_dict()))
        )
        self.assertEqual(snapshot, restored)


class PrequentialTests(unittest.TestCase):
    def test_simultaneous_matches_never_train_on_each_other(self):
        rows = _biased_rows(3, same_kickoff=True)
        with patch.object(calibration, "WARMUP_MATCHES", 1):
            replay = calibration.prequential_predictions(rows)
        # All three matches share one kickoff slot, so none has prior history.
        self.assertEqual(replay, [])

    def test_strong_repeatable_miscalibration_activates(self):
        rows = _biased_rows(40)
        with patch.multiple(
            calibration,
            WARMUP_MATCHES=5,
            MIN_EVALUATED_MATCHES=20,
            MIN_COHORT_OBSERVATIONS=40,
            MIN_COHORT_MATCHES=5,
            MIN_FAMILY_OBSERVATIONS=20,
            MIN_FAMILY_MATCHES=10,
            MIN_FAMILY_CLASS=5,
        ):
            snapshot = calibration.build_snapshot(rows, bootstrap_samples=500)
        self.assertTrue(snapshot.global_gate["active"], snapshot.global_gate)
        self.assertTrue(snapshot.family_gates["goals_team_scoring"]["active"])
        self.assertTrue(snapshot.cohort_gates["cohort-a"]["active"])
        probability, value, applied, reason = snapshot.apply(
            95, "goals_team_scoring", "cohort-a"
        )
        self.assertTrue(applied)
        self.assertLess(value, 95)
        self.assertEqual(reason, "calibrated")
        self.assertAlmostEqual(probability, snapshot.model.probability(
            0.95, "goals_team_scoring", "cohort-a"
        ))

    def test_ineligible_cohort_stays_identity(self):
        snapshot = calibration.build_snapshot(
            _biased_rows(8), created_at="2026-06-27T00:00:00Z", bootstrap_samples=20,
        )
        probability, value, applied, reason = snapshot.apply(
            67, "goals_team_scoring", "new-cohort"
        )
        self.assertEqual((probability, value, applied), (0.67, 67, False))
        self.assertIn("global", reason)


def _biased_rows(matches: int, *, same_kickoff: bool = False):
    """95%/5% raw prices whose repeated empirical rates are 75%/25%."""
    rows = []
    for match in range(matches):
        kickoff = "2026-06-01T00:00:00Z" if same_kickoff else (
            f"2026-06-{match + 1:02d}T00:00:00Z"
        )
        outcomes = (1, 1, 1, 0, 0, 0, 0, 1)
        probabilities = (95, 95, 95, 95, 5, 5, 5, 5)
        for market, (outcome, probability) in enumerate(zip(outcomes, probabilities)):
            rows.append(calibration.CalibrationObservation(
                lobby_id="lobby",
                match_id=f"match-{match}",
                kickoff=kickoff,
                market_id=f"market-{match}-{market}",
                question="Will there be 3 or more total goals?",
                raw_probability_int=probability,
                outcome=outcome,
                family="goals_team_scoring",
                cohort="cohort-a",
            ))
    return rows


if __name__ == "__main__":
    unittest.main()
