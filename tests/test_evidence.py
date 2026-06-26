import unittest

from bot.evidence import build_match_evidence
from bot.pipeline import MatchResult
from bot.pricing import PriceCtx


def _af_h2h_books():
    return [{
        "name": "Bet365",
        "bets": [{"id": 1, "values": [
            {"value": "Home", "odd": "2.00"},
            {"value": "Draw", "odd": "3.50"},
            {"value": "Away", "odd": "4.00"},
        ]}],
    }]


def _oa_h2h_books():
    return [{
        "key": "draftkings",
        "title": "DraftKings",
        "markets": [{"key": "h2h", "outcomes": [
            {"name": "Home", "price": 1.95},
            {"name": "Draw", "price": 3.6},
            {"name": "Away", "price": 4.1},
        ]}],
    }]


class _OA:
    def __init__(self):
        self.requested = []

    def event_odds(self, _event_id, markets):
        self.requested.append(tuple(markets))
        return _oa_h2h_books() if markets == ["h2h"] else []


class EvidenceTests(unittest.TestCase):
    def test_direct_mapped_odds_include_provider_bookmaker_and_raw_prices(self):
        result = _result({
            "win": {"market": "match_winner", "subject": "home",
                    "comparator": "win", "threshold": None, "period": "match"},
        })
        ctx = PriceCtx("Home", "Away", _af_h2h_books(), _OA(), {"id": "event"})
        evidence = build_match_evidence(result, ctx, lineups=None, minutes_before=30)

        q = evidence["question_evidence"][0]
        self.assertEqual(q["market_id"], "win")
        self.assertGreaterEqual(len(q["direct_odds"]), 2)
        sources = {obs["source"] for obs in q["direct_odds"]}
        books = {obs["bookmaker"] for obs in q["direct_odds"]}
        self.assertEqual(sources, {"api-football", "odds-api"})
        self.assertIn("Bet365", books)
        self.assertIn("DraftKings", books)
        self.assertTrue(all(obs["raw_odds"] for obs in q["direct_odds"]))

    def test_unmapped_question_receives_related_odds_for_audit(self):
        result = _result({
            "odd": {"market": "none", "subject": "match",
                    "comparator": "yes", "threshold": None, "period": "match"},
        }, question="Will something unusual happen?")
        ctx = PriceCtx("Home", "Away", _af_h2h_books(), _OA(), {"id": "event"})
        evidence = build_match_evidence(result, ctx, lineups=None, minutes_before=30)

        q = evidence["question_evidence"][0]
        self.assertEqual(q["direct_odds"], [])
        self.assertTrue(q["related_odds"])
        self.assertTrue(all(obs["why_relevant"] for obs in q["related_odds"]))


def _result(intents, question="Will Home win the match?"):
    market_id = next(iter(intents))
    return MatchResult(
        sp_match={"id": "match", "name": "Home vs Away",
                  "opening_time": "2026-06-22T17:00:00Z"},
        fixture={"fixture": {"id": 42}},
        home="Home",
        away="Away",
        markets=[{"id": market_id, "question": question}],
        intents=intents,
    )


if __name__ == "__main__":
    unittest.main()
