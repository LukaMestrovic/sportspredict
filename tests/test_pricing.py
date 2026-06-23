import unittest

from bot.pricing import PriceCtx, price_intent


def _scorer_intent(player):
    return {"market": "player_goal_scorer", "subject": "player", "player": player,
            "comparator": "yes", "threshold": None, "period": "match"}


class _FakeOddsAPI:
    """Returns canned anytime-scorer books; records the markets it was asked for."""

    def __init__(self, books):
        self._books = books
        self.requested = []

    def event_odds(self, _event_id, markets):
        self.requested.append(tuple(markets))
        return self._books


def _scorer_books(quotes):
    """quotes: {player: decimal_yes_price}. One book, anytime-scorer market."""
    return [{
        "key": "book",
        "markets": [{
            "key": "player_goal_scorer_anytime",
            "outcomes": [
                {"name": "Yes", "description": player, "price": price}
                for player, price in quotes.items()
            ],
        }],
    }]


# A lone, stale API-Football anytime-scorer book that still prices a benched
# player short — the exact shape that produced the bogus ~51% quote.
def _af_scorer_books(player, odd):
    return [{
        "name": "Bet365",
        "bets": [{"id": 92, "values": [{"value": player, "odd": str(odd)}]}],
    }]


class PlayerPropCascadeTests(unittest.TestCase):
    def test_starter_priced_from_odds_api_consensus(self):
        oa = _FakeOddsAPI(_scorer_books({"Bruno Fernandes": 3.5}))
        ctx = PriceCtx(home="Portugal", away="Uzbekistan", af_books=[],
                       oa=oa, oa_event={"id": "e"})
        out, src, _ = price_intent(_scorer_intent("Bruno Fernandes"), ctx)
        self.assertEqual(src, "odds-api")
        self.assertGreater(out["probability"], 0.0)

    def test_benched_player_skips_instead_of_using_stale_af_book(self):
        # Odds API quotes the anytime-scorer market for others, but not Ramos,
        # while API-Football still carries a lone stale 1.80 line for him.
        oa = _FakeOddsAPI(_scorer_books({"Bruno Fernandes": 3.5}))
        ctx = PriceCtx(home="Portugal", away="Uzbekistan",
                       af_books=_af_scorer_books("Goncalo Ramos", 1.80),
                       oa=oa, oa_event={"id": "e"})
        out, src, _ = price_intent(_scorer_intent("Gonçalo Ramos"), ctx)
        self.assertIsNone(out)
        self.assertIsNone(src)

    def test_falls_back_to_af_when_market_not_offered_at_all(self):
        # Odds API offers no anytime-scorer market for the event: the lone AF
        # book is then the only source and is used.
        oa = _FakeOddsAPI([])
        ctx = PriceCtx(home="Portugal", away="Uzbekistan",
                       af_books=_af_scorer_books("Goncalo Ramos", 1.80),
                       oa=oa, oa_event={"id": "e"})
        out, src, _ = price_intent(_scorer_intent("Gonçalo Ramos"), ctx)
        self.assertEqual(src, "api-football")
        self.assertIsNotNone(out)


if __name__ == "__main__":
    unittest.main()
