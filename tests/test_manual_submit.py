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

    def test_lineup_marker_written_before_platform_submission(self):
        session_path, response_path = self._files(lineups_available=True)
        verification = {"ok": True, "checked": 1, "expected": 1,
                        "missing": [], "mismatched": [], "ignored_closed": []}
        outcome = {"submitted": 1, "updated": 0, "unchanged": 0, "failed": 0,
                   "platform_verification": verification}
        events = []
        with self._patched_submit(outcome, events=events) as patched:
            with redirect_stdout(StringIO()) as out:
                manual_submit._submit(SimpleNamespace(
                    session=str(session_path),
                    response=str(response_path),
                    response_stdin=False,
                ))

        patched["submit"].assert_called_once()
        self.assertEqual(events, ["marker", "submit", "marker"])
        self.assertEqual(patched["marker"].call_count, 2)
        first_kwargs = patched["marker"].call_args_list[0].kwargs
        self.assertEqual(first_kwargs["source"], "manual-chatgpt-started")
        self.assertEqual(first_kwargs["metadata"]["phase"], "manual_submit_started")
        self.assertTrue(first_kwargs["metadata"]["lineups_available"])
        marker_kwargs = patched["marker"].call_args.kwargs
        self.assertEqual(marker_kwargs["source"], "manual-chatgpt")
        self.assertEqual(marker_kwargs["metadata"]["phase"], "manual_submitted")
        self.assertTrue(marker_kwargs["metadata"]["lineups_available"])
        self.assertIn("PREDICTIONS_JSON=", out.getvalue())
        self.assertIn("PREDICTIONS:", out.getvalue())
        self.assertIn("Will Home win?", out.getvalue())
        self.assertIn("CRON_BLOCKED=true", out.getvalue())

    def test_no_lineup_manual_submit_does_not_write_marker(self):
        session_path, response_path = self._files(lineups_available=False)
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
        patched["marker"].assert_not_called()
        self.assertIn("CRON_MARKER_WITH_LINEUPS=false", out.getvalue())
        self.assertIn("CRON_BLOCKED=false", out.getvalue())

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
        self.assertIn("CRON_MARKER_WITH_LINEUPS=false", out.getvalue())
        self.assertIn("CRON_BLOCKED=false", out.getvalue())

    def test_prepare_with_lineups_writes_cron_marker(self):
        evidence_path = self.root / "evidence.json"
        lineups = {
            "Home": {"starting_xi": [f"H{i}" for i in range(11)], "bench": []},
            "Away": {"starting_xi": [f"A{i}" for i in range(11)], "bench": []},
        }
        evidence_json = {
            "evidence_hash": "hash",
            "match": {"match_id": "match", "home": "Home", "away": "Away",
                      "kickoff": "2099-06-22T17:00:00Z", "lineups": lineups},
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

        events = []

        def write_marker(*_args, **kwargs):
            events.append(kwargs["metadata"]["phase"])
            return self.root / "marker.done"

        with patch.object(manual_submit, "MANUAL_DIR", self.root / "manual"), \
             patch.object(manual_submit, "_nonblocking_lock", return_value=_no_lock()), \
             patch.object(manual_submit, "_next_match",
                          return_value=(sp, {"id": "event"}, {"id": "lobby"},
                                        result.sp_match, kickoff)), \
             patch.object(manual_submit, "APIFootball", _AF), \
             patch.object(manual_submit, "OddsAPI", lambda **_kw: object()), \
             patch.object(manual_submit.lineup_fetcher, "fetch_lineups",
                          return_value=[
                              {"team": {"name": "Home"},
                               "startXI": [{"player": {"name": f"H{i}"}} for i in range(11)]},
                              {"team": {"name": "Away"},
                               "startXI": [{"player": {"name": f"A{i}"}} for i in range(11)]},
                          ]), \
             patch.object(manual_submit.submission_state, "write_marker",
                          side_effect=write_marker) as marker, \
             patch.object(manual_submit, "run_match", return_value=result), \
             redirect_stdout(StringIO()) as out:
            manual_submit._prepare(SimpleNamespace(
                next=True, fresh=True, require_lineups=True,
            ))

        self.assertEqual(events, ["manual_prepare_started", "manual_prepare_ready"])
        self.assertEqual(marker.call_count, 2)
        self.assertIn("LINEUPS_AVAILABLE=true", out.getvalue())
        self.assertIn("CRON_MARKER_PATH=", out.getvalue())
        self.assertIn("CRON_MARKER_WITH_LINEUPS=true", out.getvalue())
        self.assertIn("CRON_BLOCKED=true", out.getvalue())

    def test_manual_submit_allows_repeated_submission_attempts(self):
        session_path, response_path = self._files(lineups_available=True)
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
        self.assertEqual(patched["marker"].call_count, 2)

    def _files(self, *, lineups_available: bool = False):
        evidence_path = self.root / "evidence.json"
        lineups = (
            {
                "Home": {"starting_xi": [f"H{i}" for i in range(11)]},
                "Away": {"starting_xi": [f"A{i}" for i in range(11)]},
            }
            if lineups_available else None
        )
        evidence_path.write_text(json.dumps({
            "evidence_hash": "hash",
            "match": {"match_id": "match", "home": "Home", "away": "Away",
                      "kickoff": "2099-06-22T17:00:00Z", "lineups": lineups},
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
            "lineups_available": lineups_available,
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
    def _patched_submit(self, outcome, *, events: list[str] | None = None):
        def apply_response(result, *_args, **_kwargs):
            result.predictions = [
                Prediction("m", "Will Home win?", 0.5, 50, 0, "manual")
            ]
            result.llm_pricing_audit_path = str(self.root / "audit.json")
            result.llm_pricing_report_path = str(self.root / "audit.md")
            return result

        def submit_side_effect(*_args, **_kwargs):
            if events is not None:
                events.append("submit")
            return outcome, ["run"]

        def marker_side_effect(*_args, **_kwargs):
            if events is not None:
                events.append("marker")
            return self.root / "marker.done"

        with patch.object(manual_submit, "_nonblocking_lock", return_value=_no_lock()), \
             patch.object(manual_submit, "SportPredict", return_value=object()), \
             patch.object(manual_submit.llm_pricing, "apply_pricing_response",
                          side_effect=apply_response), \
             patch.object(manual_submit, "submit_with_ledger",
                          side_effect=submit_side_effect) as submit, \
             patch.object(manual_submit.submission_state, "write_marker",
                          side_effect=marker_side_effect) as marker:
            yield {"submit": submit, "marker": marker}


if __name__ == "__main__":
    unittest.main()
