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
        llm_pricing._ask = lambda evidence, **_kw: {
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

    def test_report_surfaces_provided_context(self):
        llm_pricing._ask = lambda evidence, **_kw: {
            "briefing": "b", "sources": [], "markets": [_audit("m1", 57)],
        }
        with tempfile.TemporaryDirectory() as tmp:
            result = llm_pricing.price_match(
                _result(), _evidence(with_context=True),
                Path(tmp) / "evidence.json", 30.0, force=True,
            )
            report = Path(result.llm_pricing_report_path).read_text()

        self.assertIn("## Provided context", report)
        self.assertIn("home form:", report)
        self.assertIn("referee:", report)
        self.assertIn("home player form: 1 players", report)
        self.assertIn("structured context available: team form, player form, referee, injuries",
                      report)

    def test_report_surfaces_simulator_estimate(self):
        llm_pricing._ask = lambda evidence, **_kw: {
            "briefing": "b", "sources": [], "markets": [_audit("m1", 57)],
        }
        with tempfile.TemporaryDirectory() as tmp:
            result = llm_pricing.price_match(
                _result(), _evidence(with_simulator=True),
                Path(tmp) / "evidence.json", 30.0, force=True,
            )
            report = Path(result.llm_pricing_report_path).read_text()

        self.assertIn("simulator estimate: 23.47%", report)
        self.assertIn("any_player_threshold:goals:>:1:reg", report)
        self.assertIn("all-history Brier 0.168899", report)

    def test_missing_audit_field_skips_market(self):
        bad = _audit("m1", 57)
        bad.pop("online_odds_found")
        llm_pricing._ask = lambda evidence, **_kw: {"markets": [bad]}
        result = llm_pricing.price_match(_result(), _evidence(), None, 30.0, force=True)
        self.assertEqual(result.predictions, [])
        self.assertEqual(result.skip_reasons["m1"],
                         "LLM pricing missing audit field: online_odds_found")

    def test_leak_guard_refuses_after_kickoff(self):
        calls = []
        llm_pricing._ask = (
            lambda evidence, **_kw: calls.append(1) or {"markets": [_audit("m1", 57)]}
        )
        result = llm_pricing.price_match(_result(), _evidence(), None, -1.0, force=True)
        self.assertEqual(calls, [])
        self.assertEqual(result.predictions, [])
        self.assertEqual(result.skip_reasons["m1"], "LLM pricing refused after kickoff")

    def test_refresh_flag_reaches_cache_layer(self):
        seen = []
        llm_pricing._ask = (
            lambda evidence, **kw: seen.append(kw.get("refresh"))
            or {"markets": [_audit("m1", 57)]}
        )
        llm_pricing.price_match(
            _result(), _evidence(), None, 30.0, force=True, refresh=True,
        )
        self.assertEqual(seen, [True])


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


def _evidence(with_context=False, with_simulator=False):
    question = {
        "market_id": "m1",
        "direct_odds": [{"probability": 0.55}],
        "related_odds": [],
    }
    if with_simulator:
        question["simulator_model_estimates"] = [{
            "source": "sportspredict-simulator",
            "family": "any_player_threshold",
            "contract_key": "any_player_threshold:goals:>:1:reg",
            "probability": 0.2347,
            "probability_pct": 23.47,
            "historical_evidence": {
                "model_performance": {
                    "all_history": {"available": True, "brier": 0.168899,
                                    "always_50_brier": 0.25, "matches": 2277},
                    "wc2026": {"available": False, "reason": "no unseen settled"},
                },
                "empirical_rate": {
                    "all_history": {"available": True, "rate": 0.234701, "matches": 2974},
                    "wc2026": {"available": True, "rate": 0.382353, "matches": 34},
                },
            },
        }]
    evidence = {
        "schema_version": 5,
        "evidence_hash": "abc",
        "match": {"match_id": "match", "home": "Home", "away": "Away",
                  "kickoff": "2026-06-22T17:00:00Z"},
        "question_evidence": [question],
    }
    if with_context:
        evidence.update({
            "team_form": {"home": {"games": 3, "gf_avg": 1.7}, "away": {}},
            "player_form": {"home": [{"name": "Striker One", "sot_per90": 1.2}],
                            "away": []},
            "referee_profile": {"name": "J. Smith", "yellows_per_game": 4.0},
            "injuries": {"home": [{"player": "X", "type": "Out"}], "away": []},
        })
    return evidence


if __name__ == "__main__":
    unittest.main()
