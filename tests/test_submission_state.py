import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from bot import submission_state


class SubmissionStateTests(unittest.TestCase):
    def test_evidence_lineups_detects_two_confirmed_starting_xis(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "evidence.json"
            path.write_text(json.dumps({
                "match": {
                    "lineups": {
                        "Home": {"starting_xi": [str(i) for i in range(11)]},
                        "Away": {"starting_xi": [str(i) for i in range(11)]},
                    },
                },
            }))
            self.assertTrue(submission_state.evidence_has_lineups(path))

    def test_empty_or_missing_lineups_do_not_count(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "evidence.json"
            path.write_text(json.dumps({"match": {"lineups": None}}))
            self.assertFalse(submission_state.evidence_has_lineups(path))

    def test_marker_without_lineups_does_not_block(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp)
            kickoff = datetime(2099, 6, 22, 17, tzinfo=timezone.utc)
            submission_state.write_marker(
                "match", kickoff, 30,
                source="manual-chatgpt",
                metadata={"lineups_available": False},
                state_dir=state_dir,
            )
            self.assertFalse(
                submission_state.marker_with_lineups_exists(
                    "match", kickoff, 30, state_dir=state_dir,
                )
            )
            self.assertTrue(
                submission_state.marker_exists(
                    "match", kickoff, 30, state_dir=state_dir,
                )
            )
            self.assertFalse(
                submission_state.marker_blocks_cron(
                    "match", kickoff, 30, state_dir=state_dir,
                )
            )

    def test_marker_with_lineups_blocks(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp)
            kickoff = datetime(2099, 6, 22, 17, tzinfo=timezone.utc)
            submission_state.write_marker(
                "match", kickoff, 30,
                source="manual-chatgpt",
                metadata={"lineups_available": True},
                state_dir=state_dir,
            )
            self.assertTrue(
                submission_state.marker_with_lineups_exists(
                    "match", kickoff, 30, state_dir=state_dir,
                )
            )
            self.assertTrue(
                submission_state.marker_blocks_cron(
                    "match", kickoff, 30, state_dir=state_dir,
                )
            )

    def test_cron_marker_blocks_even_without_lineups(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp)
            kickoff = datetime(2099, 6, 22, 17, tzinfo=timezone.utc)
            submission_state.write_marker(
                "match", kickoff, 30,
                source="cron",
                metadata={"lineups_available": False},
                state_dir=state_dir,
            )
            self.assertTrue(
                submission_state.marker_blocks_cron(
                    "match", kickoff, 30, state_dir=state_dir,
                )
            )


if __name__ == "__main__":
    unittest.main()
