import unittest
from unittest.mock import patch

from bot.pipeline import (
    MatchResult,
    Prediction,
    _COMPOUND_RE,
    run_match,
    submit_predictions,
    submit_with_ledger,
)


class CompoundDetectionTests(unittest.TestCase):
    def test_uppercase_logical_operator(self):
        self.assertIsNotNone(_COMPOUND_RE.search("Will A happen OR B happen?"))

    def test_lowercase_logical_operator(self):
        question = (
            "Will Jordan score the first goal of the game and Algeria score "
            "in the second half?"
        )
        self.assertIsNotNone(_COMPOUND_RE.search(question))

    def test_non_compound_player_exclusion_is_ignored(self):
        self.assertIsNone(_COMPOUND_RE.search("Will Harry Kane score (excluding own goals)?"))

    def test_threshold_or_is_not_a_logical_operator(self):
        self.assertIsNone(_COMPOUND_RE.search("Will Austria be caught offside 2 or more times?"))

    def test_team_name_and_is_not_a_logical_operator(self):
        question = "Will Bosnia and Herzegovina receive more cards than Qatar?"
        self.assertIsNone(_COMPOUND_RE.search(question))


class SkipReasonTests(unittest.TestCase):
    def test_unmapped_intent_has_specific_reason(self):
        fixture = {
            "fixture": {"id": 1},
            "teams": {"home": {"name": "Home"}, "away": {"name": "Away"}},
        }
        af = _AF(fixture)
        market = {"id": "m", "question": "Will something unsupported happen?"}
        with patch("bot.pipeline.parse_questions", return_value={
            "m": {"market": "none", "subject": "match"}
        }), patch("bot.pipeline.external.estimate", return_value=(None, None)):
            result = run_match(
                {"name": "HOME vs AWAY", "opening_time": "2026-01-01T00:00:00Z"},
                [market], af, None,
            )
        self.assertEqual(result.skipped[0][1], "parser marked unsupported")

    def test_external_fallback_can_be_disabled_for_backtests(self):
        fixture = {
            "fixture": {"id": 1},
            "teams": {"home": {"name": "Home"}, "away": {"name": "Away"}},
        }
        market = {"id": "m", "question": "Will something unsupported happen?"}
        with patch("bot.pipeline.parse_questions", return_value={}), patch(
            "bot.pipeline.external.estimate"
        ) as estimate:
            result = run_match(
                {"name": "Home vs Away", "opening_time": "2026-01-01T00:00:00Z"},
                [market],
                _AF(fixture),
                allow_external=False,
            )
        estimate.assert_not_called()
        self.assertEqual(len(result.skipped), 1)
        self.assertEqual(result.markets, [market])
        self.assertEqual(result.skip_reasons["m"], "parser returned no intent")


class SubmissionTests(unittest.TestCase):
    def test_predictions_are_submitted_in_api_sized_batches(self):
        sp = _SP()
        predictions = [
            Prediction(str(i), "question", 0.5, 50, 1, "label")
            for i in range(51)
        ]
        result = MatchResult({}, None, None, None, predictions=predictions)
        batch = submit_predictions(sp, "lobby", [result])
        self.assertEqual([len(part) for part in sp.batches], [50, 1])
        self.assertEqual(batch[0], {
            "market_id": "0", "lobby_id": "lobby", "probability": 50,
        })

    def test_recorded_submission_marks_ledger_after_success(self):
        sp = _SP()
        result = MatchResult(
            {"id": "match", "name": "Home vs Away",
             "opening_time": "2026-06-22T17:00:00Z"},
            None, "Home", "Away",
            predictions=[Prediction("m", "question", 0.5, 50, 1, "label")],
        )
        with patch("bot.pipeline.ledger.record_run", return_value="run") as record, patch(
            "bot.pipeline.ledger.mark_submitted"
        ) as submitted:
            batch, run_ids = submit_with_ledger(
                sp, "event", "lobby", [result],
                window_min=5, minutes_before=4.8,
            )
        record.assert_called_once_with("event", "lobby", result, 5, 4.8)
        submitted.assert_called_once_with("run")
        self.assertEqual(run_ids, ["run"])
        self.assertEqual(batch[0]["market_id"], "m")


class _AF:
    def __init__(self, fixture):
        self.fixture = fixture

    def find_fixture(self, *_args):
        return self.fixture

    def odds(self, _fixture_id):
        return []


class _SP:
    def __init__(self):
        self.batches = []

    def submit_batch(self, batch):
        self.batches.append(batch)


if __name__ == "__main__":
    unittest.main()
