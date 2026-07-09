import unittest

from bot import card_stoppage


class CardStoppageTests(unittest.TestCase):
    def test_expected_total_cards_from_api_football_total_cards(self):
        result = card_stoppage.expected_total_cards_from_af_books([
            {"name": "Book A", "bets": [{"id": 80, "values": [
                {"value": "Over 4.5", "odd": "2.00"},
                {"value": "Under 4.5", "odd": "2.00"},
            ]}]},
            {"name": "Book B", "bets": [{"id": 80, "values": [
                {"value": "Over 4.5", "odd": "1.80"},
                {"value": "Under 4.5", "odd": "2.10"},
            ]}]},
        ])

        self.assertEqual(result["source"], "api-football")
        self.assertEqual(result["market_key"], "af_bet_80")
        self.assertEqual(result["line"], 4.5)
        self.assertEqual(result["book_count"], 2)
        self.assertGreater(result["expected_total_cards"], 4.0)
        self.assertLess(result["expected_total_cards"], 6.5)

    def test_expected_total_cards_from_oddsapi_total_cards(self):
        result = card_stoppage.expected_total_cards_from_oddsapi_books([
            {"title": "Book A", "markets": [{
                "key": "alternate_totals_cards",
                "outcomes": [
                    {"name": "Over", "point": 5.5, "price": 2.20},
                    {"name": "Under", "point": 5.5, "price": 1.70},
                ],
            }]},
        ])

        self.assertEqual(result["source"], "odds-api")
        self.assertEqual(result["market_key"], "alternate_totals_cards")
        self.assertEqual(result["line"], 5.5)
        self.assertEqual(result["book_count"], 1)
        self.assertGreater(result["expected_total_cards"], 4.0)

    def test_fit_model_is_centered_and_capped(self):
        rows = []
        for index in range(60):
            total_cards = 2 + (index % 7)
            rows.append({
                "total_cards": total_cards,
                "outcome": total_cards >= 5,
            })

        model = card_stoppage.fit_model(rows)
        low = card_stoppage.predict_from_model(model, 1.0)
        high = card_stoppage.predict_from_model(model, 12.0)

        self.assertTrue(model["available"])
        self.assertGreater(model["beta"], 0)
        self.assertEqual(model["observations"], 60)
        self.assertAlmostEqual(low["base_probability"], model["empirical_rate"])
        self.assertLessEqual(
            abs(high["logit_adjustment"]),
            card_stoppage.MAX_ABS_LOGIT_ADJUSTMENT,
        )
        self.assertGreater(high["probability"], low["probability"])


if __name__ == "__main__":
    unittest.main()
