import unittest

from bot.matcher import match_intent


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

    def test_card_comparison_uses_yellow_card_1x2(self):
        full = self.match(market="team_cards", subject="home", comparator="more", period="match")
        half = self.match(market="cards_compare", subject="away", comparator="more", period="2H")
        self.assertEqual((full["bet_id"], full["value"]), (158, "Home"))
        self.assertEqual((half["bet_id"], half["value"]), (162, "Away"))

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


if __name__ == "__main__":
    unittest.main()
