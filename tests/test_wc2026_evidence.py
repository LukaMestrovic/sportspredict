import tempfile
import unittest
from pathlib import Path

from bot import wc2026_evidence


def _fixture(fid, date, status="FT", round_name="Group Stage - 1"):
    return {
        "fixture": {"id": fid, "date": date, "status": {"short": status}},
        "league": {"round": round_name},
        "teams": {"home": {"id": fid * 2}, "away": {"id": fid * 2 + 1}},
    }


def _event(kind, minute, *, detail="", team_id=1, extra=0, comments=None):
    return {
        "type": kind, "detail": detail, "comments": comments,
        "time": {"elapsed": minute, "extra": extra}, "team": {"id": team_id},
    }


class _AF:
    def __init__(self):
        self.calls = []
        self._fixtures = [
            _fixture(1, "2026-06-20T18:00:00Z"),
            _fixture(2, "2026-06-29T20:30:00Z", status="PEN", round_name="Round of 32"),
            _fixture(3, "2026-06-30T01:00:00Z", status="FT", round_name="Round of 32"),
            _fixture(4, "2026-06-29T21:00:00Z", status="NS", round_name="Round of 32"),
        ]
        self.events = {
            1: [_event("Goal", 70), _event("Card", 42, detail="Red Card")],
            2: [
                _event("Goal", 105),
                _event("Card", 112, detail="Red Card"),
                _event("Card", 75, detail="Yellow Card", team_id=2),
            ],
        }

    def fixtures(self):
        return self._fixtures

    def settled_events(self, fixture_id):
        self.calls.append(fixture_id)
        return self.events[fixture_id]


class WC2026EvidenceTests(unittest.TestCase):
    def test_refresh_is_target_scoped_and_exposes_knockout_rates(self):
        af = _AF()
        with tempfile.TemporaryDirectory() as directory:
            snapshot = wc2026_evidence.refresh(
                af, "2026-06-30T01:00:00Z",
                {"goal_window:after_second_hydration:et", "red_card:match"},
                path=Path(directory) / "wc.json",
            )

        self.assertEqual(af.calls, [1, 2])
        self.assertEqual(snapshot["eligible_matches"], 2)
        self.assertTrue(snapshot["complete"])
        late = snapshot["contracts"]["goal_window:after_second_hydration:et"]
        self.assertEqual(late["wc2026"]["rate"], 1.0)
        self.assertEqual(late["wc2026_knockout"]["matches"], 1)
        red = snapshot["contracts"]["red_card:match"]
        self.assertEqual(red["wc2026"]["yes_events"], 2)
        self.assertEqual(red["wc2026_knockout"]["rate"], 1.0)

    def test_regulation_late_goal_excludes_extra_time(self):
        af = _AF()
        with tempfile.TemporaryDirectory() as directory:
            snapshot = wc2026_evidence.refresh(
                af, "2026-06-30T01:00:00Z",
                {"goal_window:after_second_hydration:reg"},
                path=Path(directory) / "wc.json",
            )
        rate = snapshot["contracts"]["goal_window:after_second_hydration:reg"]["wc2026"]
        self.assertEqual(rate["yes_events"], 1)
        self.assertEqual(rate["rate"], 0.5)

    def test_overlay_preserves_static_history(self):
        estimates = {"m": {
            "contract_key": "red_card:match",
            "historical_evidence": {
                "empirical_rate": {"all_history": {"available": True, "rate": 0.13}},
            },
        }}
        snapshot = {"generated_at": "now", "contracts": {"red_card:match": {
            "wc2026": {"available": True, "rate": 0.12},
            "wc2026_knockout": {"available": False},
        }}}
        wc2026_evidence.overlay(estimates, snapshot)
        rates = estimates["m"]["historical_evidence"]["empirical_rate"]
        self.assertEqual(rates["all_history"]["rate"], 0.13)
        self.assertEqual(rates["wc2026"]["rate"], 0.12)


if __name__ == "__main__":
    unittest.main()
