import unittest

from bot import predictor as afpred
from bot.matcher import match_intent


def _I(market, subject="match", comparator="yes", threshold=None, period="match",
       player=None):
    return {"market": market, "subject": subject, "comparator": comparator,
            "threshold": threshold, "period": period, "player": player}


class DirectContractTests(unittest.TestCase):
    def match(self, **intent):
        return match_intent(intent, "Home Team", "Away Team")

    def test_halftime_draw_uses_first_half_winner_contract(self):
        self.assertEqual(
            self.match(market="match_draw", subject="match", comparator="yes", period="1H"),
            {"type": "select", "bet_id": 13, "value": "Draw", "label": "draw 1H"},
        )

    def test_second_half_goal_comparison_uses_second_half_winner(self):
        spec = self.match(market="match_winner", subject="away", comparator="win", period="2H")
        self.assertEqual((spec["bet_id"], spec["value"]), (3, "Away"))

    def test_card_comparison_uses_yellow_card_proxy_for_full_match(self):
        full = self.match(market="team_cards", subject="home", comparator="more", period="match")
        half = self.match(market="cards_compare", subject="away", comparator="more", period="2H")
        self.assertEqual(
            (full["type"], full["bet_id"], full["value"]),
            ("select", 158, "Home"),
        )
        self.assertEqual(
            full["contract_proxy"],
            "yellow_cards_1x2_for_all_cards_compare",
        )
        self.assertIn("yellow-card", full["proxy_note"])
        self.assertIsNone(half)

    def test_half_corner_comparison_uses_half_1x2(self):
        spec = self.match(
            market="team_corners", subject="away", comparator="more", period="1H"
        )
        self.assertEqual((spec["bet_id"], spec["value"]), (130, "Away"))

    def test_half_goal_comparison_uses_half_winner(self):
        spec = self.match(
            market="team_score_2h", subject="home", comparator="more", period="2H"
        )
        self.assertEqual((spec["bet_id"], spec["value"]), (3, "Home"))

    def test_numeric_offside_comparison_is_repaired_to_team_total(self):
        spec = self.match(
            market="offsides_compare", subject="home", comparator="gte",
            threshold=2, period="match",
        )
        self.assertEqual((spec["bet_id"], spec["side"], spec["line"]), (167, "Over", 1.5))

    def test_highest_scoring_half_ignores_spurious_half_period(self):
        spec = self.match(
            market="highest_scoring_half_2h", subject="match", comparator="yes", period="2H"
        )
        self.assertEqual((spec["bet_id"], spec["value"]), (11, "2nd Half"))

    def test_total_shots_on_target_uses_total_contract(self):
        spec = self.match(
            market="total_shots_on_target", subject="match", comparator="gte",
            threshold=8, period="match",
        )
        self.assertEqual((spec["bet_id"], spec["side"], spec["line"]), (87, "Over", 7.5))

    def test_team_shots_on_target_total_has_no_catalog_contract(self):
        spec = self.match(
            market="team_shots_on_target", subject="home", comparator="gte",
            threshold=4, period="match",
        )
        self.assertIsNone(spec)

    def test_first_team_to_score_is_not_team_to_score(self):
        spec = self.match(
            market="first_team_to_score", subject="home", comparator="yes", period="match"
        )
        self.assertEqual((spec["bet_id"], spec["value"]), (14, "Home"))

    def test_full_match_first_goal_accepts_narrow_regulation_proxy(self):
        intent = _I("first_team_to_score", "home")
        intent["time_scope"] = "full_match"
        knockout = match_intent(intent, "Home", "Away", stage="knockout")
        group = match_intent(intent, "Home", "Away", stage="group")
        self.assertEqual(knockout["bet_id"], 14)
        self.assertEqual(
            knockout["scope_proxy"],
            "regulation_first_team_to_score_for_full_match",
        )
        self.assertNotIn("scope_proxy", group)

    def test_own_goal_uses_exact_yes_no_contract(self):
        spec = self.match(
            market="own_goal", subject="match", comparator="yes", period="match"
        )
        self.assertEqual(
            (spec["type"], spec["bet_id"], spec["value"]),
            ("select", 59, "Yes"),
        )

    def test_team_score_excluding_own_goals_uses_labeled_scoreboard_proxy(self):
        intent = _I("team_score", "away")
        intent["excludes_own_goals"] = True
        spec = match_intent(intent, "Home", "Away")
        self.assertEqual((spec["type"], spec["bet_id"], spec["value"]),
                         ("select", 44, "Yes"))
        self.assertEqual(
            spec["contract_proxy"],
            "team_to_score_for_team_score_excluding_own_goals",
        )
        self.assertIn("own goals", spec["proxy_note"])


class KnockoutMarketMappingTests(unittest.TestCase):
    def test_new_exact_markets_map_to_expected_bets(self):
        cases = [
            (_I("to_advance", "home"), {"type": "select", "bet_id": 61, "value": "Home"}),
            (_I("to_advance", "away"), {"type": "select", "bet_id": 61, "value": "Away"}),
            (_I("team_clean_sheet", "home"), {"type": "select", "bet_id": 27, "value": "Yes"}),
            (_I("team_clean_sheet", "away"), {"type": "select", "bet_id": 28, "value": "Yes"}),
            (_I("team_score_both_halves", "home"), {"type": "select", "bet_id": 111, "value": "Yes"}),
            (_I("both_teams_card"), {"type": "select", "bet_id": 252, "value": "Yes"}),
            (_I("penalty_awarded"), {"type": "select", "bet_id": 163, "value": "Yes"}),
            (_I("red_card"), {"type": "ou", "bet_id": 335, "side": "Over", "line": 0.5}),
            (_I("total_shots", "match", "gte", 22), {"type": "ou", "bet_id": 211, "side": "Over", "line": 21.5}),
            (_I("team_shots", "home", "gte", 10), {"type": "ou", "bet_id": 221, "side": "Over", "line": 9.5}),
        ]
        for intent, expected in cases:
            spec = match_intent(intent, "Home FC", "Away FC")
            self.assertIsNotNone(spec, msg=intent)
            for key, value in expected.items():
                self.assertEqual(spec[key], value, msg=f"{intent} -> {key}")

    def test_win_margin_maps_to_asian_handicap_pair(self):
        spec = match_intent(_I("win_margin", "home", "gte", 2), "Home FC", "Away FC")
        self.assertEqual(spec["type"], "ah")
        self.assertEqual((spec["bet_id"], spec["side"], spec["line"]), (4, "Home", 1.5))
        self.assertIsNone(match_intent(_I("win_margin", "match", "gte", 2), "H", "A"))

    def test_total_shots_for_one_team_becomes_team_shots(self):
        spec = match_intent(_I("total_shots", "home", "gte", 12), "Home FC", "Away FC")
        self.assertEqual((spec["type"], spec["bet_id"]), ("ou", 221))


class AsianHandicapDevigTests(unittest.TestCase):
    def _book(self):
        return {"name": "b", "bets": [{"id": 4, "values": [
            {"value": "Home -1.5", "odd": "3.00"},
            {"value": "Away -1.5", "odd": "1.40"},
            {"value": "Home -0.5", "odd": "2.00"},   # other ladder lines: ignored
            {"value": "Away -0.5", "odd": "1.80"},
        ]}]}

    def test_devig_isolates_the_requested_pair(self):
        spec = {"type": "ah", "bet_id": 4, "side": "Home", "line": 1.5, "label": "x"}
        out = afpred.predict([self._book(), self._book()], spec)
        self.assertIsNotNone(out)
        # fair Home -1.5 = (1/3.0)/(1/3.0 + 1/1.4) ~ 0.318, not blended with -0.5.
        self.assertAlmostEqual(out["probability"], 0.3186, places=3)

    def test_devig_ignores_opposite_handicap_row(self):
        book = {"name": "b", "bets": [{"id": 4, "values": [
            {"value": "Home -1.5", "odd": "2.00"},
            {"value": "Away -1.5", "odd": "1.73"},
            {"value": "Home +1.5", "odd": "1.01"},
            {"value": "Away +1.5", "odd": "13.00"},
        ]}]}
        spec = {"type": "ah", "bet_id": 4, "side": "Home", "line": 1.5, "label": "x"}
        out = afpred.predict([book], spec)
        self.assertIsNotNone(out)
        # Regression: do not pair Home -1.5 with Away +1.5, which would be ~87%.
        self.assertAlmostEqual(out["probability"], 0.464, places=3)

    def test_away_win_margin_uses_positive_home_handicap_row(self):
        book = {"name": "b", "bets": [{"id": 4, "values": [
            {"value": "Home +1.5", "odd": "1.18"},
            {"value": "Away +1.5", "odd": "4.50"},
            {"value": "Home -1.5", "odd": "4.00"},
            {"value": "Away -1.5", "odd": "1.22"},
        ]}]}
        spec = {"type": "ah", "bet_id": 4, "side": "Away", "line": 1.5, "label": "x"}
        obs = afpred.observations([book], spec)
        self.assertEqual(obs[0]["contract"], "Away +1.5")
        self.assertAlmostEqual(obs[0]["probability"], 0.2077, places=3)

    def test_missing_pair_returns_no_price(self):
        spec = {"type": "ah", "bet_id": 4, "side": "Home", "line": 2.5, "label": "x"}
        self.assertIsNone(afpred.predict([self._book()], spec))


if __name__ == "__main__":
    unittest.main()
