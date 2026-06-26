import tempfile
import unittest
from pathlib import Path

from bot import llm_pricing
from bot.pipeline import MatchResult


class LLMFinalPricingTests(unittest.TestCase):
    def setUp(self):
        llm_pricing.config.OPENAI_API_KEY = "test-key"
        self._orig_ask = llm_pricing._ask

    def tearDown(self):
        llm_pricing._ask = self._orig_ask

    def test_complete_market_audit_creates_prediction_and_report(self):
        llm_pricing._ask = lambda evidence: {
            "briefing": "Home should control territory.",
            "sources": ["https://example.com/preview"],
            "markets": [_audit("m1", 57)],
        }
        with tempfile.TemporaryDirectory() as tmp:
            result = llm_pricing.price_match(
                _result(),
                _evidence(),
                Path(tmp) / "evidence.json",
                30.0,
                force=True,
            )

        self.assertEqual(len(result.predictions), 1)
        pred = result.predictions[0]
        self.assertEqual(pred.probability_int, 57)
        self.assertEqual(pred.source, "llm-pricing")
        self.assertEqual(pred.llm_reasoning_summary, "odds plus lineup support 57%.")
        self.assertEqual(result.skipped, [])
        self.assertTrue(Path(result.llm_pricing_audit_path).exists())
        self.assertTrue(Path(result.llm_pricing_report_path).exists())

    def test_missing_audit_field_skips_market(self):
        bad = _audit("m1", 57)
        bad.pop("online_odds_found")
        llm_pricing._ask = lambda evidence: {"markets": [bad]}
        result = llm_pricing.price_match(_result(), _evidence(), None, 30.0, force=True)
        self.assertEqual(result.predictions, [])
        self.assertEqual(result.skip_reasons["m1"],
                         "LLM pricing missing audit field: online_odds_found")

    def test_leak_guard_refuses_after_kickoff(self):
        calls = []
        llm_pricing._ask = lambda evidence: calls.append(1) or {"markets": [_audit("m1", 57)]}
        result = llm_pricing.price_match(_result(), _evidence(), None, -1.0, force=True)
        self.assertEqual(calls, [])
        self.assertEqual(result.predictions, [])
        self.assertEqual(result.skip_reasons["m1"], "LLM pricing refused after kickoff")


def _audit(market_id, probability):
    return {
        "market_id": market_id,
        "probability_int": probability,
        "provided_odds_used": [{"source": "api-football", "bookmaker": "Bet365"}],
        "online_odds_found": [],
        "non_odds_factors_used": [{"factor": "lineup", "source": "provided evidence"}],
        "ignored_or_downweighted_evidence": [],
        "reasoning_summary": "odds plus lineup support 57%.",
        "sources": ["https://example.com/preview"],
    }


def _result():
    return MatchResult(
        sp_match={"id": "match", "name": "Home vs Away",
                  "opening_time": "2026-06-22T17:00:00Z"},
        fixture={"fixture": {"id": 42}},
        home="Home",
        away="Away",
        markets=[{"id": "m1", "question": "Will Home win the match?"}],
    )


def _evidence():
    return {
        "schema_version": 1,
        "evidence_hash": "abc",
        "match": {"match_id": "match", "home": "Home", "away": "Away",
                  "kickoff": "2026-06-22T17:00:00Z"},
        "question_evidence": [{
            "market_id": "m1",
            "direct_odds": [{"probability": 0.55}],
            "related_odds": [],
        }],
    }


if __name__ == "__main__":
    unittest.main()
