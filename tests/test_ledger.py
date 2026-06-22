import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
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
        self.assertEqual(
            json.loads(questions[0]["book_probabilities_json"]), [0.61, 0.65]
        )
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
        with closing(ledger.connect(self.path)) as db:
            run = db.execute("SELECT * FROM runs WHERE id = 'run-2'").fetchone()
        self.assertEqual(run["status"], "submitted")
        self.assertEqual(run["submitted_at"], "2026-06-22T16:55:00+00:00")

    def test_settles_both_windows_from_explicit_real_outcome(self):
        for run_id, window, result in (
            ("run-30", 30, _result(0.30, 30)),
            ("run-5", 5, _result(0.40, 40)),
        ):
            ledger.record_run(
                "event", "lobby", result, window, window - 0.5,
                path=self.path, run_id=run_id,
            )
            ledger.mark_submitted(run_id, path=self.path)

        official = [{
            "id": "result", "market_id": "a", "market_status": "settled",
            "probability_submitted": 40, "brier_score": 0.36,
            "created_date": "2026-06-22T16:55:00Z",
        }]
        stats = ledger.settle_results(
            {"a": 1}, official, path=self.path,
            settled_at="2026-06-22T20:00:00Z",
        )
        again = ledger.settle_results({"a": 1}, official, path=self.path)

        with closing(ledger.connect(self.path)) as db:
            rows = db.execute(
                "SELECT * FROM questions WHERE market_id = 'a' ORDER BY run_id"
            ).fetchall()
            skipped = db.execute(
                "SELECT outcome FROM questions WHERE market_id = 'b'"
            ).fetchall()
        self.assertEqual(stats, {
            "settled_predictions": 2, "remaining_predictions": 0,
        })
        self.assertEqual(again["settled_predictions"], 0)
        self.assertEqual([row["outcome"] for row in rows], [1, 1])
        self.assertAlmostEqual(rows[0]["brier_score"], 0.49)
        self.assertAlmostEqual(rows[1]["brier_score"], 0.36)
        self.assertEqual(rows[1]["result_probability_int"], 40)
        self.assertTrue(all(row["outcome"] is None for row in skipped))

        summary = ledger.performance(path=self.path)
        overall = next(row for row in summary if row["group"] == "overall")
        self.assertEqual(overall["predictions"], 2)
        self.assertAlmostEqual(overall["mean_brier"], 0.425)


def _result(probability=0.634, probability_int=63):
    markets = [
        {"id": "a", "question": "Will Home win the match?"},
        {"id": "b", "question": "Will something unusual happen?"},
    ]
    prediction = Prediction(
        "a", markets[0]["question"], probability, probability_int, 4, "Home win",
        book_probabilities=[0.61, 0.65],
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
