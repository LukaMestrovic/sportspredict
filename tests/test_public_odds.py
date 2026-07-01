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


if __name__ == "__main__":
    unittest.main()
