import unittest
from unittest.mock import patch

from bot import public_odds


LISTING = """
<div class="ch_line" data-id="85242000">
  <div class="ch_l c_name">
    USA (shots on target) - Bosnia and Herzegovina (shots on target)
    <span class="c_match_num">#702</span>
  </div>
</div>
"""


DETAIL = """
<div>TEAM TOTAL</div>
<div>USA (shots on target) (4.5) under</div><span>2.35</span>
<div>USA (shots on target) (4.5) over</div><span>1.53</span>
<div>Bosnia and Herzegovina (shots on target) (2.5) under</div><span>2.04</span>
<div>Bosnia and Herzegovina (shots on target) (2.5) over</div><span>1.71</span>
<div>USA (shots on target) (5.5) under</div><span>1.68</span>
<div>USA (shots on target) (5.5) over</div><span>2.08</span>
"""


class PublicOddsTests(unittest.TestCase):
    def test_betolimp_team_sot_total_is_extracted_and_devigged(self):
        def fetch(url):
            return DETAIL if url.endswith("-85242000") else LISTING

        intent = {
            "market": "team_shots_on_target",
            "subject": "home",
            "comparator": "gte",
            "threshold": 6,
            "period": "match",
        }
        with patch("bot.public_odds._fetch", side_effect=fetch):
            odds = public_odds.online_odds(intent, "USA", "Bosnia & Herzegovina")

        self.assertEqual(len(odds), 1)
        candidate = odds[0]
        self.assertEqual(candidate["bookmaker"], "BetOlimp")
        self.assertIn("usa-shots-on-target-bosnia-and-herzegovina", candidate["url"])
        self.assertEqual(candidate["contract"], "USA (shots on target) (5.5) over")
        self.assertEqual(candidate["quoted_price_or_odds"],
                         "USA (shots on target) (5.5) under 1.68; "
                         "USA (shots on target) (5.5) over 2.08")
        self.assertAlmostEqual(candidate["probability_pct"], 44.68)

    def test_hydration_goal_special_single_sided_price_is_extracted(self):
        page = """
        <div>Match Specials</div>
        <div>Goal scored before the 1st half hydration break</div><span>2.25</span>
        """
        intent = {"market": "goal_window", "subject": "match", "period": "match"}
        with patch("bot.public_odds.SPECIAL_PAGES", [
                ("BetVictor", "betvictor_match_specials", "https://example.test/specials")
        ]), patch("bot.public_odds._fetch", return_value=page):
            odds = public_odds.online_odds(
                intent, "Canada", "Morocco",
                question="Will a goal be scored before the first hydration break?",
            )

        self.assertEqual(len(odds), 1)
        candidate = odds[0]
        self.assertEqual(candidate["bookmaker"], "BetVictor")
        self.assertEqual(candidate["contract"],
                         "Goal scored before the 1st half hydration break")
        self.assertEqual(candidate["devig_method"], "raw single-sided implied probability")
        self.assertAlmostEqual(candidate["probability_pct"], 44.44)

    def test_after_second_hydration_special_single_sided_price_is_extracted(self):
        page = """
        <div>Match Specials</div>
        <div>Goal scored after the 2nd half hydration break</div><span>2.05</span>
        """
        intent = {"market": "goal_window", "subject": "match", "period": "match"}
        with patch("bot.public_odds.SPECIAL_PAGES", [
                ("BetVictor", "betvictor_match_specials", "https://example.test/specials")
        ]), patch("bot.public_odds._fetch", return_value=page):
            odds = public_odds.online_odds(
                intent, "Mexico", "England",
                question="Will a goal be scored after the second hydration break?",
            )

        self.assertEqual(len(odds), 1)
        candidate = odds[0]
        self.assertEqual(candidate["bookmaker"], "BetVictor")
        self.assertEqual(candidate["contract"],
                         "Goal scored after the 2nd half hydration break")
        self.assertEqual(candidate["devig_method"], "raw single-sided implied probability")
        self.assertAlmostEqual(candidate["probability_pct"], 48.78)

    def test_after_first_hydration_does_not_use_second_hydration_label(self):
        page = """
        <div>Match Specials</div>
        <div>Goal scored after the 2nd half hydration break</div><span>2.05</span>
        """
        intent = {"market": "goal_window", "subject": "match", "period": "match"}
        with patch("bot.public_odds.SPECIAL_PAGES", [
                ("BetVictor", "betvictor_match_specials", "https://example.test/specials")
        ]), patch("bot.public_odds._fetch", return_value=page):
            odds = public_odds.online_odds(
                intent, "Argentina", "Egypt",
                question="Will a goal be scored in the first half after the first hydration break?",
            )

        self.assertEqual(odds, [])

    def test_penalty_or_red_yes_no_special_is_extracted_with_question_text(self):
        page = """
        <div>Penalty or Red card</div>
        <div>Yes</div><span>1.80</span>
        <div>No</div><span>1.95</span>
        """
        with patch("bot.public_odds.SPECIAL_PAGES", [
                ("BetOlimp", "betolimp_match_specials", "https://example.test/match")
        ]), patch("bot.public_odds._fetch", return_value=page):
            odds = public_odds.online_odds(
                {"market": "none"}, "Canada", "Morocco",
                question="Will there be a penalty kick OR red card?",
            )

        self.assertEqual(len(odds), 1)
        candidate = odds[0]
        self.assertEqual(candidate["contract"], "Penalty or Red card")
        self.assertEqual(candidate["devig_method"], "same-book special yes/no de-vig")
        self.assertAlmostEqual(candidate["probability_pct"], 52.0, places=1)

    def test_substitute_score_only_special_not_used_for_score_or_assist(self):
        page = """
        <div>Match Specials</div>
        <div>A substitute to score</div><span>4.00</span>
        """
        with patch("bot.public_odds.SPECIAL_PAGES", [
                ("BetVictor", "betvictor_match_specials", "https://example.test/specials")
        ]), patch("bot.public_odds._fetch", return_value=page):
            odds = public_odds.online_odds(
                {"market": "substitute_score_or_assist"}, "Switzerland", "Colombia",
                question="Will a substitute score or assist a goal?",
            )

        self.assertEqual(odds, [])


if __name__ == "__main__":
    unittest.main()
