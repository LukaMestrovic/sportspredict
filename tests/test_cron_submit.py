"""Dispatcher tests for scripts/cron_submit.py.

Pure stdlib, no network: SportPredict and the per-match worker are monkeypatched.
The regression of record is the simultaneous-kickoff case — two matches sharing a
kickoff must BOTH be processed in one tick, not just the soonest one.

    python -m unittest tests.test_cron_submit
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts import cron_submit  # noqa: E402


class _FakeSP:
    def __init__(self, matches):
        self._matches = matches

    def event(self):
        return {"id": "e1"}

    def lobby(self, _event_id):
        return {"id": "l1"}

    def matches(self, _event_id, _lobby_id):
        return self._matches


class DispatchTest(unittest.TestCase):
    def setUp(self):
        # Isolate marker/lock state to a temp dir so real runs aren't disturbed.
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self._orig = (cron_submit.STATE_DIR, cron_submit.LOCK_PATH,
                      cron_submit.SportPredict, cron_submit._process_match,
                      cron_submit.calibration.sync_and_refit,
                      cron_submit.calibration.load_active_snapshot,
                      cron_submit.WebAPI, sys.argv)
        cron_submit.STATE_DIR = root / "state"
        cron_submit.LOCK_PATH = root / "submit.lock"
        sys.argv = ["cron_submit"]
        self.processed: list[str] = []
        self.synced: list[str] = []
        cron_submit.calibration.sync_and_refit = (
            lambda sp, web, event, lobby: self.synced.append(lobby["id"]) or {
                "observations_imported": 0, "observations_total": 0,
                "refit": False, "model_id": None, "global_gate": None,
            }
        )
        cron_submit.calibration.load_active_snapshot = lambda _lobby_id: None
        cron_submit.WebAPI = lambda: object()
        cron_submit._process_match = (
            lambda sp_match, kickoff, *a, **k: self.processed.append(sp_match["name"])
        )

    def tearDown(self):
        (cron_submit.STATE_DIR, cron_submit.LOCK_PATH, cron_submit.SportPredict,
         cron_submit._process_match, cron_submit.calibration.sync_and_refit,
         cron_submit.calibration.load_active_snapshot, cron_submit.WebAPI,
         sys.argv) = self._orig
        self._tmp.cleanup()

    def _match(self, name, mins_to_ko):
        ko = datetime.now(timezone.utc) + timedelta(minutes=mins_to_ko)
        return {"id": name, "name": name,
                "opening_time": ko.strftime("%Y-%m-%dT%H:%M:%S.000Z")}

    def _run(self, matches):
        cron_submit.SportPredict = lambda: _FakeSP(matches)
        cron_submit.main()

    def test_simultaneous_kickoffs_both_processed(self):
        # Two matches at the same ~T-30 kickoff: BOTH must be processed this tick.
        self._run([self._match("A vs B", 30), self._match("C vs D", 30)])
        self.assertEqual(sorted(self.processed), ["A vs B", "C vs D"])

    def test_far_matches_are_skipped(self):
        # A due match is processed; a far one in the same tick is not.
        self._run([self._match("A vs B", 29), self._match("C vs D", 300)])
        self.assertEqual(self.processed, ["A vs B"])
        self.assertEqual(self.synced, ["l1"])

    def test_nothing_due_processes_nothing(self):
        self._run([self._match("A vs B", 120), self._match("C vs D", 200)])
        self.assertEqual(self.processed, [])
        self.assertEqual(self.synced, ["l1"])

    def test_status_and_dry_run_do_not_sync_calibration(self):
        sys.argv = ["cron_submit", "--status"]
        self._run([self._match("A vs B", 120)])
        self.assertEqual(self.synced, [])

        sys.argv = ["cron_submit", "--dry-run"]
        self._run([self._match("A vs B", 30)])
        self.assertEqual(self.synced, [])


class ProcessMatchTest(unittest.TestCase):
    def setUp(self):
        self._orig = (
            cron_submit.APIFootball, cron_submit.OddsAPI, cron_submit.run_match,
            cron_submit.submit_with_ledger,
        )

    def tearDown(self):
        (
            cron_submit.APIFootball, cron_submit.OddsAPI, cron_submit.run_match,
            cron_submit.submit_with_ledger,
        ) = self._orig

    def test_cron_fire_refreshes_odds_lineups_and_llm_pricing(self):
        seen = {}

        class _AF:
            def __init__(self, *, refresh_odds=False):
                seen["af_refresh"] = refresh_odds

            def find_fixture(self, opening_time, name):
                return {"fixture": {"id": 42}}

            def lineups(self, fixture_id):
                seen["lineups_fixture_id"] = fixture_id
                return [{"team": {"name": "A"}}]

        class _OA:
            def __init__(self, *, refresh_odds=False):
                seen["oa_refresh"] = refresh_odds

        def _run_match(*args, **kwargs):
            seen["run_match_kwargs"] = kwargs
            return SimpleNamespace(predictions=[], skipped=[])

        cron_submit.APIFootball = _AF
        cron_submit.OddsAPI = _OA
        cron_submit.run_match = _run_match
        sp = SimpleNamespace(markets=lambda lobby_id, match_id: [])
        kickoff = datetime.now(timezone.utc) + timedelta(minutes=30)
        cron_submit._process_match(
            {"id": "m1", "name": "A vs B",
             "opening_time": kickoff.strftime("%Y-%m-%dT%H:%M:%S.000Z")},
            kickoff,
            datetime.now(timezone.utc),
            sp,
            {"id": "event"},
            {"id": "lobby"},
            SimpleNamespace(dry_run=True),
        )

        self.assertTrue(seen["af_refresh"])
        self.assertTrue(seen["oa_refresh"])
        self.assertEqual(seen["lineups_fixture_id"], 42)
        self.assertTrue(seen["run_match_kwargs"]["llm_pricing_enabled"])
        self.assertTrue(seen["run_match_kwargs"]["llm_pricing_refresh"])


if __name__ == "__main__":
    unittest.main()
