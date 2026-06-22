import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from bot import ledger
from bot.pipeline import MatchResult, Prediction


class LedgerRecordingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "ledger.sqlite3"

    def tearDown(self):
        self.tmp.cleanup()

    def test_records_predictions_skips_and_raw_inputs(self):
        result = _result()
        run_id = ledger.record_run(
            "event", "lobby", result, 30, 29.4, path=self.path,
            run_id="run-1", recorded_at="2026-06-22T16:30:00+00:00",
        )
        self.assertEqual(run_id, "run-1")

        db = sqlite3.connect(self.path)
        db.row_factory = sqlite3.Row
        run = db.execute("SELECT * FROM runs").fetchone()
        questions = db.execute(
            "SELECT * FROM questions ORDER BY market_id"
        ).fetchall()
        db.close()

        self.assertEqual(run["status"], "priced")
        self.assertEqual(run["match_id"], "match")
        self.assertEqual(json.loads(run["af_odds_json"]), [{"book": "af"}])
        self.assertEqual(json.loads(run["oa_odds_json"]), [{"book": "oa"}])
        self.assertEqual(len(questions), 2)
        self.assertEqual(questions[0]["probability_int"], 63)
        self.assertEqual(json.loads(questions[0]["intent_json"])["market"], "match_winner")
        self.assertEqual(json.loads(questions[0]["market_spec_json"])["bet_id"], 1)
        self.assertEqual(questions[1]["skip_reason"], "no direct market mapping")

    def test_submission_status_is_updated(self):
        ledger.record_run(
            "event", "lobby", _result(), 5, 4.5, path=self.path,
            run_id="run-2",
        )
        ledger.mark_submitted(
            "run-2", path=self.path,
            submitted_at="2026-06-22T16:55:00+00:00",
        )
        with ledger.connect(self.path) as db:
            run = db.execute("SELECT * FROM runs WHERE id = 'run-2'").fetchone()
        self.assertEqual(run["status"], "submitted")
        self.assertEqual(run["submitted_at"], "2026-06-22T16:55:00+00:00")


def _result():
    markets = [
        {"id": "a", "question": "Will Home win the match?"},
        {"id": "b", "question": "Will something unusual happen?"},
    ]
    prediction = Prediction(
        "a", markets[0]["question"], 0.634, 63, 4, "Home win",
    )
    return MatchResult(
        sp_match={
            "id": "match", "name": "HOME vs AWAY",
            "opening_time": "2026-06-22T17:00:00Z",
        },
        fixture={"fixture": {"id": 42}}, home="Home", away="Away",
        predictions=[prediction],
        skipped=[(markets[1]["question"], "no direct market mapping")],
        markets=markets,
        intents={
            "a": {"market": "match_winner", "subject": "home"},
            "b": {"market": "none", "subject": "match"},
        },
        market_specs={"a": {"type": "select", "bet_id": 1}, "b": None},
        skip_reasons={"b": "no direct market mapping"},
        af_books=[{"book": "af"}],
        oa_observations=[{"book": "oa"}],
    )


if __name__ == "__main__":
    unittest.main()
