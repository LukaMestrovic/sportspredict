import json
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from bot import evidence
from bot.pipeline import MatchResult, Prediction
from scripts import codex_workflow


OPENING = "2099-06-22T17:00:00Z"
MATCH = {"id": "match", "name": "Home vs Away", "opening_time": OPENING}
FIXTURE = {
    "fixture": {"id": 42},
    "teams": {"home": {"name": "Home"}, "away": {"name": "Away"}},
}
MARKETS = [{"id": "m", "question": "Will something genuinely unfamiliar happen?"}]


class CodexWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_unfamiliar_question_stops_before_paid_odds_work(self):
        parsed = SimpleNamespace(
            unresolved=[{
                "market_id": "m", "question": MARKETS[0]["question"],
                "normalized_question": MARKETS[0]["question"],
            }],
        )
        with self._selection(), \
             patch.object(codex_workflow, "RUNS_DIR", self.root), \
             patch.object(codex_workflow, "APIFootball", return_value=_AF()), \
             patch.object(codex_workflow.question_parser, "parse_questions", return_value=parsed), \
             patch.object(codex_workflow, "OddsAPI",
                          side_effect=AssertionError("Odds API fetched before intents")), \
             patch.object(codex_workflow, "prepare_match",
                          side_effect=AssertionError("evidence built before intents")), \
             redirect_stdout(StringIO()) as output:
            codex_workflow._prepare(SimpleNamespace(next=True, match=None, fresh=True))

        text = output.getvalue()
        self.assertIn("STATUS=needs_intents", text)
        request_path = _printed_path(text, "INTENT_REQUEST_PATH")
        request = json.loads(request_path.read_text())
        self.assertEqual(request["match"]["id"], "match")
        self.assertEqual(request["unresolved"][0]["market_id"], "m")

    def test_finish_prepare_records_missing_lineup_warning_and_hashes(self):
        parsed = SimpleNamespace(
            unresolved=[], intent_sources={"m": "tracked-rule"},
            resolution_provenance={},
        )
        evidence_payload = {
            "evidence_hash": "abc",
            "match": {"match_id": "match", "home": "Home", "away": "Away",
                      "kickoff": OPENING, "lineups": None},
            "question_evidence": [{
                "market_id": "m", "question": "Will Home win?",
                "intent": {"market": "match_winner"},
                "direct_market_spec": None, "direct_odds": [],
            }],
        }
        result = MatchResult(
            sp_match=MATCH, fixture=FIXTURE, home="Home", away="Away",
            markets=[{"id": "m", "question": "Will Home win?"}],
            evidence_json=evidence_payload, evidence_hash="abc",
            af_books=[{"bookmaker": "book"}], oa_observations=[],
        )

        def prepare(*_args, evidence_directory=None, **_kwargs):
            path = evidence_directory / "evidence.json"
            path.write_text(json.dumps(evidence_payload))
            result.evidence_path = str(path)
            return result

        with patch.object(codex_workflow, "RUNS_DIR", self.root), \
             patch.object(
                 codex_workflow.lineup_fetcher, "fetch_lineups",
                 side_effect=RuntimeError("lineup provider down"),
             ), \
             patch.object(codex_workflow, "OddsAPI", return_value=object()), \
             patch.object(codex_workflow, "prepare_match", side_effect=prepare), \
             redirect_stdout(StringIO()) as output:
            run_dir, session_id = codex_workflow._new_run_directory(MATCH)
            codex_workflow._finish_prepare(
                sp=object(), event={"id": "event"}, lobby={"id": "lobby"},
                match=MATCH, kickoff=codex_workflow._parse_kickoff(OPENING),
                markets=result.markets, af=_AF(), fixture=FIXTURE, parsed=parsed,
                run_dir=run_dir, session_id=session_id, fresh=True,
            )

        manifest_path = _printed_path(output.getvalue(), "SESSION_PATH")
        manifest = json.loads(manifest_path.read_text())
        self.assertFalse(manifest["lineups_available"])
        self.assertIn("unavailable", manifest["lineup_warning"])
        self.assertIn("lineup lookup failed", manifest["lineup_warning"])
        self.assertEqual(manifest["intent_sources"], {"m": "tracked-rule"})
        for name in ("evidence", "provider_snapshot", "task", "prompt"):
            reference = manifest["artifacts"][name]
            self.assertEqual(
                reference["sha256"],
                codex_workflow._sha256(Path(reference["path"])),
            )

    def test_manifest_artifact_tampering_fails_closed(self):
        path = self.root / "evidence.json"
        path.write_text("before")
        reference = codex_workflow._artifact_ref(path)
        path.write_text("after")
        with self.assertRaisesRegex(ValueError, "hash verification"):
            codex_workflow._verify_artifact(reference, "evidence")

    def test_ambiguous_match_selection_fails_with_candidates(self):
        sp = _SP(matches=[
            {**MATCH, "id": "one", "name": "Home vs Away A"},
            {**MATCH, "id": "two", "name": "Home vs Away B"},
        ])
        with patch.object(codex_workflow, "SportPredict", return_value=sp):
            with self.assertRaisesRegex(SystemExit, "ambiguous.*candidates"):
                codex_workflow._select_match(next_match=False, query="Home vs Away")

    def test_legacy_session_is_still_accepted_for_submission(self):
        evidence_path = self.root / "evidence.json"
        evidence_payload = {
            "schema_version": 23,
            "match": {
                "match_id": "match", "home": "Home", "away": "Away",
                "kickoff": OPENING,
            },
            "question_evidence": [],
        }
        evidence_payload["evidence_hash"] = evidence.evidence_hash(evidence_payload)
        evidence_path.write_text(json.dumps(evidence_payload))
        response_path = self.root / "response.json"
        response_path.write_text("{}")
        session_path = self.root / "legacy.json"
        session_path.write_text(json.dumps({
            "schema_version": 1, "event_id": "event", "lobby_id": "lobby",
            "match": MATCH, "fixture": FIXTURE, "home": "Home", "away": "Away",
            "minutes_before": 60.0, "markets": [], "intents": {},
            "market_specs": {}, "skip_reasons": {}, "af_books": [],
            "oa_observations": [], "evidence_path": str(evidence_path),
            "evidence_hash": evidence_payload["evidence_hash"],
            "response_path": str(response_path),
        }))
        verification = {"ok": True, "checked": 0, "expected": 0,
                        "missing": [], "mismatched": [], "ignored_closed": []}
        outcome = {"submitted": 0, "updated": 0, "unchanged": 1, "failed": 0,
                   "platform_verification": verification}

        def apply(result, *_args, **kwargs):
            self.assertIsNone(kwargs.get("expected_session_id"))
            result.predictions = [Prediction("m", "Will Home win?", .5, 50, 0, "manual")]
            result.codex_audit_path = str(self.root / "audit.json")
            result.codex_report_path = str(self.root / "audit.md")
            result.codex_match_read_path = str(self.root / "match.md")
            return result

        def submit(*_args, **kwargs):
            self.assertEqual(kwargs["minutes_before"], 42.5)
            self.assertEqual(kwargs["window_min"], 42)
            return outcome, ["run"]

        with patch.object(codex_workflow.codex_pricing, "apply_pricing_response",
                          side_effect=apply), \
             patch.object(codex_workflow, "SportPredict", return_value=object()), \
             patch.object(codex_workflow, "submit_with_ledger",
                          side_effect=submit), \
             patch.object(codex_workflow, "_minutes_before", return_value=42.5), \
             redirect_stdout(StringIO()) as output:
            codex_workflow._submit(SimpleNamespace(
                session=str(session_path), response=str(response_path),
                response_stdin=False,
            ))

        self.assertIn("STATUS=submitted", output.getvalue())
        self.assertNotIn("CRON_", output.getvalue())

    def test_unknown_session_schema_is_rejected_before_submission(self):
        session_path = self.root / "session.json"
        session_path.write_text(json.dumps({"schema_version": 999}))
        with self.assertRaisesRegex(ValueError, "unsupported session schema_version"):
            codex_workflow._submit(SimpleNamespace(
                session=str(session_path), response=None, response_stdin=True,
            ))

    def _selection(self):
        sp = _SP(matches=[MATCH])
        return patch.object(
            codex_workflow, "_select_match",
            return_value=(sp, {"id": "event"}, {"id": "lobby"}, MATCH,
                          codex_workflow._parse_kickoff(OPENING)),
        )


class _AF:
    def find_fixture(self, *_args):
        return FIXTURE


class _SP:
    def __init__(self, *, matches=None):
        self._matches = matches or [MATCH]

    def event(self):
        return {"id": "event"}

    def lobby(self, _event):
        return {"id": "lobby"}

    def matches(self, _event, _lobby):
        return self._matches

    def markets(self, _lobby, _match):
        return MARKETS


def _printed_path(output: str, key: str) -> Path:
    line = next(line for line in output.splitlines() if line.startswith(key + "="))
    return Path(line.split("=", 1)[1])


if __name__ == "__main__":
    unittest.main()
