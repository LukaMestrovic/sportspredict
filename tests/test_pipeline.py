import unittest
from unittest.mock import patch

from bot.derive import is_compound_question
from bot.pipeline import (
    MatchResult,
    Prediction,
    run_match,
    submit_predictions,
    submit_with_ledger,
)


class CompoundDetectionTests(unittest.TestCase):
    def test_uppercase_logical_operator(self):
        self.assertTrue(is_compound_question("Will A happen OR B happen?"))

    def test_lowercase_logical_operator(self):
        question = (
            "Will Jordan score the first goal of the game and Algeria score "
            "in the second half?"
        )
        self.assertTrue(is_compound_question(question))

    def test_non_compound_player_exclusion_is_ignored(self):
        self.assertFalse(is_compound_question("Will Harry Kane score (excluding own goals)?"))

    def test_threshold_or_is_not_a_logical_operator(self):
        self.assertFalse(is_compound_question("Will Austria be caught offside 2 or more times?"))

    def test_team_name_and_is_not_a_logical_operator(self):
        question = "Will Bosnia and Herzegovina receive more cards than Qatar?"
        self.assertFalse(is_compound_question(question))


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
                llm_pricing_enabled=False,
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
                llm_pricing_enabled=False,
            )
        estimate.assert_not_called()
        self.assertEqual(len(result.skipped), 1)
        self.assertEqual(result.markets, [market])
        self.assertEqual(result.skip_reasons["m"], "parser returned no intent")


class SubmissionTests(unittest.TestCase):
    def test_new_markets_are_created_in_api_sized_batches(self):
        sp = _SP()  # no existing predictions -> everything is a fresh create
        predictions = [
            Prediction(str(i), "question", 0.5, 50, 1, "label")
            for i in range(51)
        ]
        result = MatchResult({}, None, None, None, predictions=predictions)
        summary = submit_predictions(sp, "lobby", [result])
        self.assertEqual([len(part) for part in sp.batches], [50, 1])
        self.assertEqual(summary["payload"][0], {
            "market_id": "0", "lobby_id": "lobby", "probability": 50,
        })
        self.assertEqual(summary["submitted"], 51)
        self.assertEqual(summary["updated"], 0)

    def test_upsert_patches_existing_and_creates_only_new(self):
        # "a" exists at 40 -> PATCH to 50; "b" exists at 50 -> unchanged;
        # "c" has no prediction -> POST as new.
        sp = _SP(existing=[
            {"market_id": "a", "id": "pa", "probability": 40, "market_status": "open"},
            {"market_id": "b", "id": "pb", "probability": 50, "market_status": "open"},
        ])
        preds = [Prediction(m, "q", 0.5, 50, 1, "l") for m in ("a", "b", "c")]
        result = MatchResult({}, None, None, None, predictions=preds)
        summary = submit_predictions(sp, "lobby", [result])
        self.assertEqual(sp.batches, [[
            {"market_id": "c", "lobby_id": "lobby", "probability": 50}]])
        self.assertEqual(sp.updated, [("pa", 50)])      # only the moved one
        self.assertEqual(
            (summary["submitted"], summary["updated"], summary["unchanged"],
             summary["failed"]), (1, 1, 1, 0))

    def test_rejected_create_is_counted_failed(self):
        sp = _SP(submit_response={"succeeded": 0, "failed": 1, "results": [
            {"market_id": "m", "success": False, "error": "already exists"}]})
        result = MatchResult({}, None, None, None,
                             predictions=[Prediction("m", "q", 0.5, 50, 1, "l")])
        summary = submit_predictions(sp, "lobby", [result])
        self.assertEqual((summary["submitted"], summary["failed"]), (0, 1))

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
            summary, run_ids = submit_with_ledger(
                sp, "event", "lobby", [result],
                window_min=5, minutes_before=4.8,
            )
        record.assert_called_once_with("event", "lobby", result, 5, 4.8)
        submitted.assert_called_once_with("run")
        self.assertEqual(run_ids, ["run"])
        self.assertEqual(summary["payload"][0]["market_id"], "m")

    def test_ledger_marked_failed_when_nothing_lands(self):
        sp = _SP(submit_response={"succeeded": 0, "failed": 1, "results": [
            {"market_id": "m", "success": False, "error": "already exists"}]})
        result = MatchResult(
            {"id": "match", "name": "Home vs Away",
             "opening_time": "2026-06-22T17:00:00Z"},
            None, "Home", "Away",
            predictions=[Prediction("m", "question", 0.5, 50, 1, "label")],
        )
        with patch("bot.pipeline.ledger.record_run", return_value="run"), patch(
            "bot.pipeline.ledger.mark_submitted"
        ) as submitted, patch("bot.pipeline.ledger.mark_failed") as failed:
            submit_with_ledger(sp, "event", "lobby", [result],
                               window_min=5, minutes_before=4.8)
        failed.assert_called_once()
        submitted.assert_not_called()


class _AF:
    def __init__(self, fixture):
        self.fixture = fixture

    def find_fixture(self, *_args):
        return self.fixture

    def odds(self, _fixture_id):
        return []


class _SP:
    def __init__(self, existing=None, submit_response=None):
        self.batches = []
        self.updated = []
        self._existing = existing or []
        self._submit_response = submit_response

    def submit_batch(self, batch):
        self.batches.append(batch)
        if self._submit_response is not None:
            return self._submit_response
        return {"succeeded": len(batch), "failed": 0,
                "results": [{"market_id": p["market_id"], "success": True}
                            for p in batch]}

    def list_predictions(self, lobby_id):
        return self._existing

    def update_prediction(self, prediction_id, probability):
        self.updated.append((prediction_id, probability))
        return {"id": prediction_id, "probability": probability}


if __name__ == "__main__":
    unittest.main()
