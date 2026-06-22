import unittest

from bot.predictor import predict


class PlayerPropTests(unittest.TestCase):
    def test_single_sided_shots_on_target_shape(self):
        books = [_book(242, [
            {"value": "Marcel Sabitzer - 1+", "odd": "2.00"},
            {"value": "Marcel Sabitzer - 2+", "odd": "5.00"},
        ])]
        out = predict(books, {
            "type": "player_threshold", "bet_id": 242, "player": "Marcel Sabitzer",
            "side": "Over", "line": 0.5, "label": "player SoT",
        })
        self.assertAlmostEqual(out["probability"], 0.46)

    def test_player_name_match_is_accent_insensitive(self):
        books = [_book(92, [{"value": "Luka Sucic", "odd": "4.00"}])]
        out = predict(books, {
            "type": "player_yes", "bet_id": 92, "player": "Luka Sučić",
            "label": "player scorer",
        })
        self.assertAlmostEqual(out["probability"], 0.23)

    def test_single_book_extreme_player_price_is_not_discarded(self):
        books = [_book(251, [{"value": "Harry Kane", "odd": "13.50"}])]
        out = predict(books, {
            "type": "player_yes", "bet_id": 251, "player": "Harry Kane",
            "label": "player booked",
        })
        self.assertIsNotNone(out)


def _book(bet_id, values):
    return {"bets": [{"id": bet_id, "values": values}]}


if __name__ == "__main__":
    unittest.main()
