import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bot import calibration, ledger
from bot.pipeline import MatchResult, Prediction


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


class SynchronizationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "ledger.sqlite3"
        self.event = {"id": "event"}
        self.lobby = {"id": "lobby"}

    def tearDown(self):
        self.tmp.cleanup()

    def test_initial_backfill_uses_official_probability_and_ignores_crowd(self):
        match = _settled_match("match-1", "2026-06-01T00:00:00Z")
        market = _settled_market("market-1", outcome=1, crowd=3)
        sp = _FakeSP([_official("market-1", 67)])
        result = calibration.sync_and_refit(
            sp, _FakeWeb([match], {"match-1": [market]}),
            self.event, self.lobby, path=self.path, bootstrap_samples=10,
        )
        observations = calibration.stored_observations("lobby", path=self.path)
        self.assertEqual(result["observations_total"], 1)
        self.assertEqual(observations[0].raw_probability_int, 67)
        self.assertEqual(observations[0].outcome, 1)
        self.assertEqual(observations[0].provenance, "legacy-official-result")

        # A second database with a contradictory crowd mean must be byte-stable.
        other_path = Path(self.tmp.name) / "other.sqlite3"
        changed = dict(market, prediction_average=99)
        calibration.sync_and_refit(
            sp, _FakeWeb([match], {"match-1": [changed]}),
            self.event, self.lobby, path=other_path, bootstrap_samples=10,
        )
        first = calibration.load_active_snapshot("lobby", path=self.path)
        second = calibration.load_active_snapshot("lobby", path=other_path)
        self.assertEqual(first.observation_hash, second.observation_hash)
        self.assertEqual(first.model_id, second.model_id)

    def test_post_cutover_uses_latest_matching_ledger_raw_value(self):
        match1 = _settled_match("match-1", "2026-06-01T00:00:00Z")
        web1 = _FakeWeb(
            [match1], {"match-1": [_settled_market("market-1", outcome=0)]}
        )
        calibration.sync_and_refit(
            _FakeSP([_official("market-1", 40)]), web1,
            self.event, self.lobby, path=self.path, bootstrap_samples=10,
        )

        for run_id, raw, recorded in (
            ("older", 61, "2026-06-02T00:10:00Z"),
            ("newer", 64, "2026-06-02T00:20:00Z"),
        ):
            result = _ledger_result("match-2", "market-2", final=55, raw=raw)
            ledger.record_run(
                "event", "lobby", result, 30, 29.0, path=self.path,
                run_id=run_id, recorded_at=recorded,
            )
            ledger.mark_submitted(run_id, path=self.path, submitted_at=recorded)

        match2 = _settled_match("match-2", "2026-06-02T00:00:00Z")
        web2 = _FakeWeb(
            [match2, match1],
            {
                "match-2": [_settled_market("market-2", outcome=1)],
                "match-1": [_settled_market("market-1", outcome=0)],
            },
        )
        calibration.sync_and_refit(
            _FakeSP([_official("market-1", 40), _official("market-2", 55)]),
            web2, self.event, self.lobby, path=self.path, bootstrap_samples=10,
        )
        observations = {
            row.market_id: row
            for row in calibration.stored_observations("lobby", path=self.path)
        }
        self.assertEqual(observations["market-2"].raw_probability_int, 64)
        self.assertEqual(observations["market-2"].official_probability_int, 55)
        self.assertEqual(observations["market-2"].source_run_id, "newer")
        self.assertEqual(observations["market-2"].provenance, "ledger-raw")

    def test_post_cutover_missing_raw_is_excluded_not_recursed(self):
        match1 = _settled_match("match-1", "2026-06-01T00:00:00Z")
        calibration.sync_and_refit(
            _FakeSP([_official("market-1", 40)]),
            _FakeWeb([match1], {"match-1": [_settled_market("market-1", 0)]}),
            self.event, self.lobby, path=self.path, bootstrap_samples=10,
        )
        match2 = _settled_match("match-2", "2026-06-02T00:00:00Z")
        result = calibration.sync_and_refit(
            _FakeSP([_official("market-1", 40), _official("market-2", 91)]),
            _FakeWeb(
                [match2, match1],
                {
                    "match-2": [_settled_market("market-2", 1)],
                    "match-1": [_settled_market("market-1", 0)],
                },
            ),
            self.event, self.lobby, path=self.path, bootstrap_samples=10,
        )
        self.assertEqual(result["observations_total"], 1)
        self.assertEqual(result["excluded"], 1)


class _FakeSP:
    def __init__(self, results):
        self._results = results

    def results(self, _lobby_id):
        return self._results


class _FakeWeb:
    def __init__(self, matches, markets):
        self.matches = matches
        self.markets = markets

    def settled_matches_page(self, _event_id, *, skip=0, limit=8):
        return self.matches[skip:skip + limit]

    def crowd_stats(self, match_id, _lobby_id):
        return self.markets[match_id]


def _settled_match(match_id, kickoff):
    return {"id": match_id, "name": match_id, "opening_time": kickoff}


def _settled_market(market_id, outcome, crowd=50):
    return {
        "id": market_id,
        "question": "Will Home win the match?",
        "current_value": outcome * 100,
        "prediction_average": crowd,
    }


def _official(market_id, probability):
    return {
        "id": f"result-{market_id}",
        "market_id": market_id,
        "market_status": "settled",
        "probability_submitted": probability,
        "brier_score": 0.0,
        "created_date": "2026-06-01T00:00:00Z",
    }


def _ledger_result(match_id, market_id, *, final, raw):
    question = "Will Home win the match?"
    prediction = Prediction(
        market_id, question, final / 100.0, final, 1, "raw price"
    )
    prediction.raw_probability = raw / 100.0
    prediction.raw_probability_int = raw
    prediction.raw_model_cohort = "llm:test"
    prediction.calibration_family = "match_result_timing"
    return MatchResult(
        sp_match={"id": match_id, "name": match_id,
                  "opening_time": "2026-06-02T00:00:00Z"},
        fixture=None,
        home="Home",
        away="Away",
        predictions=[prediction],
        markets=[{"id": market_id, "question": question}],
        intents={market_id: {"market": "match_winner", "subject": "home"}},
        market_specs={market_id: None},
    )


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
