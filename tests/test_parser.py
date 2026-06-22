import unittest

from bot.parser import _repair_intent, _repair_subject


class SubjectRepairTests(unittest.TestCase):
    def test_named_team_total_is_repaired(self):
        intent = {"market": "total_offsides", "subject": "match"}
        repaired = _repair_subject(
            "Will Austria be caught offside 2 or more times?",
            intent, "Argentina", "Austria",
        )
        self.assertEqual(repaired["subject"], "away")
        self.assertEqual(intent["subject"], "match")

    def test_provider_word_order_and_punctuation_are_tolerated(self):
        intent = {"market": "total_fouls", "subject": "match"}
        repaired = _repair_subject(
            "Will DR Congo commit 12 or more fouls?",
            intent, "Colombia", "Congo DR",
        )
        self.assertEqual(repaired["subject"], "away")

    def test_match_total_stays_match_scoped(self):
        intent = {"market": "total_cards", "subject": "match"}
        repaired = _repair_subject(
            "Will there be 4 or more total cards shown?",
            intent, "Argentina", "Austria",
        )
        self.assertEqual(repaired["subject"], "match")

    def test_literal_team_subject_is_normalized(self):
        repaired = _repair_intent(
            "Will Norway win the match?",
            {"market": "match_winner", "subject": "Norway"},
            "Norway", "France",
        )
        self.assertEqual(repaired["subject"], "home")

    def test_numeric_second_half_goals_are_not_highest_half(self):
        repaired = _repair_intent(
            "Will the second half have 2 or more total goals?",
            {"market": "highest_scoring_half_2h", "subject": "match", "comparator": "gte"},
            "Portugal", "Uzbekistan",
        )
        self.assertEqual(repaired["market"], "total_goals")

    def test_score_or_assist_is_not_reduced_to_scorer(self):
        repaired = _repair_intent(
            "Will Orkun Kökçü score or assist a goal?",
            {"market": "player_goal_scorer", "subject": "player"},
            "Türkiye", "USA",
        )
        self.assertEqual(repaired["market"], "player_score_or_assist")

    def test_numeric_offside_is_not_reduced_to_comparison(self):
        repaired = _repair_intent(
            "Will Türkiye be caught offside 2 or more times?",
            {"market": "offsides_compare", "subject": "home", "comparator": "gte"},
            "Türkiye", "USA",
        )
        self.assertEqual(repaired["market"], "team_offsides")

    def test_offside_phrase_repairs_all_required_fields(self):
        repaired = _repair_intent(
            "Will Austria be caught offside 2 or more times?",
            {"market": "none", "subject": "match", "comparator": "yes",
             "threshold": 2, "period": "2H"},
            "Argentina", "Austria",
        )
        self.assertEqual(repaired["market"], "team_offsides")
        self.assertEqual(repaired["subject"], "away")
        self.assertEqual(repaired["comparator"], "gte")
        self.assertEqual(repaired["period"], "match")


if __name__ == "__main__":
    unittest.main()
