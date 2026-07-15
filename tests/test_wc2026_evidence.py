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


def _players(*, substitute_goals=0, substitute_assists=0):
    return [{
        "players": [{
            "statistics": [{
                "games": {"substitute": True},
                "goals": {"total": substitute_goals, "assists": substitute_assists},
                "shots": {"on": 0},
            }],
        }],
    }]


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
            1: [_event("Goal", 71), _event("Card", 42, detail="Red Card")],
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

    def settled_statistics(self, fixture_id):
        return [
            {"team": {"id": fixture_id * 2}, "statistics": [
                {"type": "Shots on Goal", "value": 6},
                {"type": "Total Shots", "value": 12},
            ]},
            {"team": {"id": fixture_id * 2 + 1}, "statistics": [
                {"type": "Shots on Goal", "value": 2},
                {"type": "Total Shots", "value": 9},
            ]},
        ]

    def fixture_players(self, fixture_id):
        return []


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

    def test_second_hydration_boundary_starts_after_minute_70(self):
        af = _AF()
        af.events[1] = [_event("Goal", 70)]
        af.events[2] = [_event("Goal", 71)]
        with tempfile.TemporaryDirectory() as directory:
            snapshot = wc2026_evidence.refresh(
                af, "2026-06-30T01:00:00Z",
                {"goal_window:after_second_hydration:reg"},
                path=Path(directory) / "wc.json",
            )
        rate = snapshot["contracts"]["goal_window:after_second_hydration:reg"]["wc2026"]
        self.assertEqual(rate["yes_events"], 1)
        self.assertEqual(rate["rate"], 0.5)

    def test_stoppage_card_refresh_exports_card_count_model(self):
        af = _AF()
        first_home = af._fixtures[0]["teams"]["home"]["id"]
        first_away = af._fixtures[0]["teams"]["away"]["id"]
        second_home = af._fixtures[1]["teams"]["home"]["id"]
        af.events[1] = [
            _event("Card", 45, detail="Yellow Card", team_id=first_home, extra=2),
            _event("Card", 80, detail="Yellow Card", team_id=first_away),
        ]
        af.events[2] = [
            _event("Card", 75, detail="Yellow Card", team_id=second_home),
        ]
        with tempfile.TemporaryDirectory() as directory:
            snapshot = wc2026_evidence.refresh(
                af, "2026-06-30T01:00:00Z",
                {"card_window:cards:stoppage_any:reg:>=:1"},
                path=Path(directory) / "wc.json",
            )

        contract = snapshot["contracts"]["card_window:cards:stoppage_any:reg:>=:1"]
        self.assertEqual(contract["wc2026"]["yes_events"], 1)
        self.assertEqual(contract["wc2026"]["rate"], 0.5)
        model = contract["card_stoppage_model"]
        self.assertEqual(model["training_scope"], "wc2026_settled_before_target")
        self.assertEqual(model["observations"], 2)
        self.assertEqual(model["yes_events"], 1)
        self.assertEqual(model["empirical_rate"], 0.5)
        self.assertEqual(model["mean_total_cards"], 1.5)

    def test_new_exact_event_contract_labels(self):
        fixture = _fixture(1, "2026-06-20T18:00:00Z")
        facts = wc2026_evidence._fixture_facts(
            fixture,
            events=[
                _event("Card", 12, detail="Yellow Card", team_id=2),
                _event("Goal", 12, detail="Normal Goal", team_id=3),
                _event("Goal", 45, detail="Normal Goal", team_id=2, extra=4),
                _event("Card", 105, detail="Yellow Card", team_id=3),
            ],
            statistics=None,
            players=None,
        )
        self.assertEqual(
            wc2026_evidence.labels_for_contract("goal_window:stoppage:any:reg", facts),
            [True],
        )
        self.assertEqual(
            wc2026_evidence.labels_for_contract("first_card_before_first_goal:reg", facts),
            [True],
        )

        neither = wc2026_evidence._fixture_facts(
            fixture, events=[], statistics=None, players=None,
        )
        self.assertEqual(
            wc2026_evidence.labels_for_contract("first_card_before_first_goal:reg", neither),
            [False],
        )

    def test_card_before_goal_uses_provider_sequence_for_same_clock(self):
        fixture = _fixture(1, "2026-06-20T18:00:00Z")
        goal_first = wc2026_evidence._fixture_facts(
            fixture,
            events=[
                _event("Goal", 20, detail="Normal Goal", team_id=2),
                _event("Card", 20, detail="Yellow Card", team_id=3),
            ],
            statistics=None,
            players=None,
        )
        self.assertEqual(
            wc2026_evidence.labels_for_contract(
                "first_card_before_first_goal:reg", goal_first,
            ),
            [False],
        )

    def test_first_half_after_first_hydration_boundary_starts_after_minute_22(self):
        af = _AF()
        af.events[1] = [_event("Goal", 22)]
        af.events[2] = [_event("Goal", 23)]
        with tempfile.TemporaryDirectory() as directory:
            snapshot = wc2026_evidence.refresh(
                af, "2026-06-30T01:00:00Z",
                {"goal_window:after_first_hydration_1h:reg"},
                path=Path(directory) / "wc.json",
            )
        rate = snapshot["contracts"][
            "goal_window:after_first_hydration_1h:reg"
        ]["wc2026"]
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

    def test_team_stat_contract_uses_every_labelable_team_match(self):
        af = _AF()
        with tempfile.TemporaryDirectory() as directory:
            snapshot = wc2026_evidence.refresh(
                af, "2026-06-30T01:00:00Z",
                {"count:shots_on_target:team:full:>=:6:reg"},
                path=Path(directory) / "wc.json",
            )
        rate = snapshot["contracts"][
            "count:shots_on_target:team:full:>=:6:reg"
        ]["wc2026"]
        # Fixture 2 ended on penalties, so provider full-match stats include
        # extra time and cannot label a regulation-only shots contract.
        self.assertEqual(rate["matches"], 1)
        self.assertEqual(rate["observations"], 2)
        self.assertEqual(rate["yes_events"], 1)
        self.assertTrue(rate["complete"])
        self.assertEqual(rate["population"], "all_labelable_matches")

    def test_extra_time_goals_and_cards_label_match_scope_only(self):
        fixture = _fixture(
            10, "2026-07-01T20:00:00Z", status="AET", round_name="Round of 16",
        )
        home_id = fixture["teams"]["home"]["id"]
        away_id = fixture["teams"]["away"]["id"]
        facts = wc2026_evidence._fixture_facts(
            fixture,
            events=[
                _event("Goal", 105, detail="Normal Goal", team_id=home_id),
                _event("Card", 110, detail="Yellow Card", team_id=away_id),
            ],
            statistics=None,
            players=None,
        )

        self.assertEqual(
            wc2026_evidence.labels_for_contract(
                "count:goals:team:full:>=:1:match", facts,
            ),
            [True, False],
        )
        self.assertEqual(
            wc2026_evidence.labels_for_contract(
                "count:goals:team:full:>=:1:reg", facts,
            ),
            [False, False],
        )
        self.assertEqual(
            wc2026_evidence.labels_for_contract(
                "count:cards:team:full:>=:1:match", facts,
            ),
            [False, True],
        )
        self.assertEqual(
            wc2026_evidence.labels_for_contract(
                "count:cards:team:full:>=:1:reg", facts,
            ),
            [False, False],
        )
        self.assertEqual(
            wc2026_evidence.labels_for_contract(
                "total_goals:full:>=:1:match", facts,
            ),
            [True],
        )
        self.assertEqual(
            wc2026_evidence.labels_for_contract(
                "total_goals:full:>=:1:reg", facts,
            ),
            [False],
        )
        self.assertEqual(
            wc2026_evidence.labels_for_contract("first_goal:full:et:team", facts),
            [True, False],
        )
        self.assertEqual(
            wc2026_evidence.labels_for_contract("first_goal:full:team", facts),
            [False, False],
        )
        self.assertEqual(
            wc2026_evidence.labels_for_contract("clean_sheet:match", facts),
            [True, False],
        )
        self.assertEqual(
            wc2026_evidence.labels_for_contract("clean_sheet:reg", facts),
            [True, True],
        )
        self.assertEqual(
            wc2026_evidence.labels_for_contract("compare:cards:full:match", facts),
            [False, True],
        )
        self.assertEqual(
            wc2026_evidence.labels_for_contract("compare:cards:full:reg", facts),
            [False, False],
        )

    def test_new_special_contract_labels_are_exact(self):
        fixture = _fixture(
            20, "2026-07-02T20:00:00Z", status="FT", round_name="Round of 16",
        )
        home_id = fixture["teams"]["home"]["id"]
        away_id = fixture["teams"]["away"]["id"]
        facts = wc2026_evidence._fixture_facts(
            fixture,
            events=[
                _event("Goal", 60, detail="Normal Goal", team_id=home_id),
                _event("Card", 45, detail="Yellow Card", team_id=away_id, extra=2),
                _event("Card", 80, detail="Yellow Card", team_id=home_id),
            ],
            statistics=[
                {"team": {"id": home_id}, "statistics": [
                    {"type": "Corner Kicks", "value": 7},
                    {"type": "Total Shots", "value": 14},
                ]},
                {"team": {"id": away_id}, "statistics": [
                    {"type": "Corner Kicks", "value": 4},
                    {"type": "Total Shots", "value": 10},
                ]},
            ],
            players=_players(substitute_assists=1),
        )

        self.assertEqual(
            wc2026_evidence.labels_for_contract("first_goal_half:2H:reg", facts),
            [True],
        )
        self.assertEqual(
            wc2026_evidence.labels_for_contract("exact_goal_margin:reg:1", facts),
            [True],
        )
        self.assertEqual(
            wc2026_evidence.labels_for_contract(
                "card_window:cards:each_half:reg:>=:1", facts,
            ),
            [True],
        )
        self.assertEqual(
            wc2026_evidence.labels_for_contract(
                "card_window:cards:stoppage_any:reg:>=:1", facts,
            ),
            [True],
        )
        self.assertEqual(
            wc2026_evidence.labels_for_contract("substitute_score_or_assist:reg", facts),
            [True],
        )
        self.assertEqual(
            wc2026_evidence.labels_for_contract(
                "compound:team_more_corners_and_total_shots:reg", facts,
            ),
            [True, False],
        )
        self.assertEqual(
            wc2026_evidence.labels_for_contract("win_both_halves:reg", facts),
            [False],
        )

        both_halves = wc2026_evidence._fixture_facts(
            fixture,
            events=[
                _event("Goal", 10, detail="Normal Goal", team_id=home_id),
                _event("Goal", 60, detail="Normal Goal", team_id=home_id),
            ],
            statistics=None,
            players=None,
        )
        self.assertEqual(
            wc2026_evidence.labels_for_contract("win_both_halves:reg", both_halves),
            [True],
        )


if __name__ == "__main__":
    unittest.main()
