import json
import tempfile
import unittest
from contextlib import contextmanager, redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from bot.pipeline import MatchResult, Prediction
from scripts import manual_submit


@contextmanager
def _no_lock():
    yield


class ManualSubmitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_marker_written_only_after_platform_verification(self):
        session_path, response_path = self._files()
        verification = {"ok": True, "checked": 1, "expected": 1,
                        "missing": [], "mismatched": [], "ignored_closed": []}
        outcome = {"submitted": 1, "updated": 0, "unchanged": 0, "failed": 0,
                   "platform_verification": verification}
        with self._patched_submit(outcome) as patched:
            with redirect_stdout(StringIO()) as out:
                manual_submit._submit(SimpleNamespace(
                    session=str(session_path),
                    response=str(response_path),
                    response_stdin=False,
                ))

        patched["submit"].assert_called_once()
        patched["marker"].assert_called_once()
        marker_kwargs = patched["marker"].call_args.kwargs
        self.assertFalse(marker_kwargs["metadata"]["lineups_available"])
        self.assertIn("PREDICTIONS_JSON=", out.getvalue())
        self.assertIn("PREDICTIONS:", out.getvalue())
        self.assertIn("Will Home win?", out.getvalue())
        self.assertIn("CRON_BLOCKED=true", out.getvalue())

    def test_marker_not_written_when_platform_verification_missing(self):
        session_path, response_path = self._files()
        outcome = {"submitted": 0, "updated": 0, "unchanged": 0, "failed": 0}
        with self._patched_submit(outcome) as patched:
            with self.assertRaises(SystemExit):
                manual_submit._submit(SimpleNamespace(
                    session=str(session_path),
                    response=str(response_path),
                    response_stdin=False,
                ))

        patched["submit"].assert_called_once()
        patched["marker"].assert_not_called()

    def test_prepare_continues_when_required_lineups_are_missing(self):
        evidence_path = self.root / "evidence.json"
        evidence_json = {
            "evidence_hash": "hash",
            "match": {"match_id": "match", "home": "Home", "away": "Away",
                      "kickoff": "2099-06-22T17:00:00Z", "lineups": None},
            "question_evidence": [{"market_id": "m", "direct_odds": []}],
        }
        evidence_path.write_text(json.dumps(evidence_json))
        result = MatchResult(
            sp_match={"id": "match", "name": "Home vs Away",
                      "opening_time": "2099-06-22T17:00:00Z"},
            fixture={"fixture": {"id": 42}},
            home="Home",
            away="Away",
            markets=[{"id": "m", "question": "Will Home win?"}],
            evidence_json=evidence_json,
            evidence_path=str(evidence_path),
            evidence_hash="hash",
        )
        sp = SimpleNamespace(markets=lambda _lobby, _match: result.markets)
        kickoff = manual_submit._parse_kickoff("2099-06-22T17:00:00Z")

        class _AF:
            def __init__(self, *, refresh_odds=False):
                pass

            def find_fixture(self, *_args):
                return {"fixture": {"id": 42}}

        with patch.object(manual_submit, "MANUAL_DIR", self.root / "manual"), \
             patch.object(manual_submit, "_nonblocking_lock", return_value=_no_lock()), \
             patch.object(manual_submit, "_next_match",
                          return_value=(sp, {"id": "event"}, {"id": "lobby"},
                                        result.sp_match, kickoff)), \
             patch.object(manual_submit.submission_state, "marker_exists",
                          side_effect=AssertionError("manual prepare checked marker")), \
             patch.object(manual_submit.submission_state, "submitted_run_exists",
                          side_effect=AssertionError("manual prepare checked ledger")), \
             patch.object(manual_submit, "APIFootball", _AF), \
             patch.object(manual_submit, "OddsAPI", lambda **_kw: object()), \
             patch.object(manual_submit.lineup_fetcher, "fetch_lineups",
                          return_value=[]), \
             patch.object(manual_submit, "run_match", return_value=result), \
             redirect_stdout(StringIO()) as out:
            manual_submit._prepare(SimpleNamespace(
                next=True, fresh=True, require_lineups=True,
            ))

        self.assertIn("LINEUPS_AVAILABLE=false", out.getvalue())
        session_line = next(
            line for line in out.getvalue().splitlines()
            if line.startswith("SESSION_PATH=")
        )
        session = json.loads(Path(session_line.split("=", 1)[1]).read_text())
        self.assertFalse(session["lineups_available"])
        self.assertIn("unavailable", session["lineup_warning"])

    def test_manual_submit_allows_repeated_submission_attempts(self):
        session_path, response_path = self._files()
        verification = {"ok": True, "checked": 1, "expected": 1,
                        "missing": [], "mismatched": [], "ignored_closed": []}
        outcome = {"submitted": 0, "updated": 1, "unchanged": 0, "failed": 0,
                   "platform_verification": verification}
        with self._patched_submit(outcome) as patched:
            with patch.object(manual_submit.submission_state, "marker_exists",
                              side_effect=AssertionError("manual submit checked marker")), \
                 patch.object(manual_submit.submission_state, "submitted_run_exists",
                              side_effect=AssertionError("manual submit checked ledger")):
                manual_submit._submit(SimpleNamespace(
                    session=str(session_path),
                    response=str(response_path),
                    response_stdin=False,
                ))

        patched["submit"].assert_called_once()
        patched["marker"].assert_called_once()

    def _files(self):
        evidence_path = self.root / "evidence.json"
        evidence_path.write_text(json.dumps({
            "evidence_hash": "hash",
            "match": {"match_id": "match", "home": "Home", "away": "Away",
                      "kickoff": "2099-06-22T17:00:00Z"},
            "question_evidence": [{"market_id": "m", "direct_odds": []}],
        }))
        response_path = self.root / "response.json"
        response_path.write_text(json.dumps({
            "briefing": "brief",
            "sources": [],
            "markets": [{
                "market_id": "m",
                "probability_int": 50,
                "provided_odds_used": [],
                "online_odds_found": [],
                "non_odds_factors_used": [],
                "ignored_or_downweighted_evidence": [],
                "reasoning_summary": "manual audit",
                "sources": [],
            }],
        }))
        session_path = self.root / "session.json"
        session_path.write_text(json.dumps({
            "event_id": "event",
            "lobby_id": "lobby",
            "match": {"id": "match", "name": "Home vs Away",
                      "opening_time": "2099-06-22T17:00:00Z"},
            "fixture": {"fixture": {"id": 42}},
            "home": "Home",
            "away": "Away",
            "minutes_before": 60.0,
            "markets": [{"id": "m", "question": "Will Home win?"}],
            "intents": {"m": {"market": "match_winner"}},
            "market_specs": {"m": None},
            "skip_reasons": {},
            "af_books": [],
            "oa_observations": [],
            "evidence_path": str(evidence_path),
            "evidence_hash": "hash",
            "response_path": str(response_path),
        }))
        return session_path, response_path

    @contextmanager
    def _patched_submit(self, outcome):
        def apply_response(result, *_args, **_kwargs):
            result.predictions = [
                Prediction("m", "Will Home win?", 0.5, 50, 0, "manual")
            ]
            result.llm_pricing_audit_path = str(self.root / "audit.json")
            result.llm_pricing_report_path = str(self.root / "audit.md")
            return result

        with patch.object(manual_submit, "_nonblocking_lock", return_value=_no_lock()), \
             patch.object(manual_submit, "SportPredict", return_value=object()), \
             patch.object(manual_submit.llm_pricing, "apply_pricing_response",
                          side_effect=apply_response), \
             patch.object(manual_submit, "submit_with_ledger",
                          return_value=(outcome, ["run"])) as submit, \
             patch.object(manual_submit.submission_state, "write_marker") as marker:
            yield {"submit": submit, "marker": marker}


if __name__ == "__main__":
    unittest.main()
