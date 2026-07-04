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
            "match_read_markdown": "# Match read\n\nHome should control territory.",
            "match_read_sources": ["https://example.com/preview"],
            "subagent_memos": _memos([_audit("m1", 57)]),
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
        self.assertTrue(Path(result.llm_match_read_path).exists())
        self.assertIn("Home should control territory",
                      Path(result.llm_match_read_path).read_text())

    def test_report_surfaces_provided_context(self):
        llm_pricing._ask = lambda evidence, **_kw: {
            **_response([_audit("m1", 57)]), "briefing": "b",
            "sources": ["https://example.com/preview"],
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
        self.assertIn("match read:", report)
        self.assertIn("language adjustment:", report)

    def test_report_surfaces_simulator_estimate(self):
        llm_pricing._ask = lambda evidence, **_kw: {
            **_response([_audit("m1", 57)]), "briefing": "b",
            "sources": ["https://example.com/preview"],
        }
        with tempfile.TemporaryDirectory() as tmp:
            result = llm_pricing.price_match(
                _result(), _evidence(with_simulator=True),
                Path(tmp) / "evidence.json", 30.0, force=True,
            )
            report = Path(result.llm_pricing_report_path).read_text()

        self.assertIn("simulator estimate: 23.47%", report)
        self.assertIn("any_player_threshold:goals:>:1:reg", report)
        self.assertIn("calibrated baseline: 23.47% from simulator", report)
        self.assertIn("contract Brier sim=0.168899 emp=0.17 50=0.25", report)

    def test_missing_audit_field_skips_market(self):
        bad = _audit("m1", 57)
        bad.pop("online_odds_found")
        llm_pricing._ask = lambda evidence, **_kw: _response([bad])
        result = llm_pricing.price_match(_result(), _evidence(), None, 30.0, force=True)
        self.assertEqual(result.predictions, [])
        self.assertEqual(result.skip_reasons["m1"],
                         "LLM pricing missing audit field: online_odds_found")

    def test_missing_match_read_skips_all_markets(self):
        llm_pricing._ask = lambda evidence, **_kw: {
            "briefing": "b", "sources": [], "markets": [_audit("m1", 57)],
        }
        result = llm_pricing.price_match(_result(), _evidence(), None, 30.0, force=True)
        self.assertEqual(result.predictions, [])
        self.assertEqual(result.skip_reasons["m1"],
                         "LLM pricing missing match_read_markdown")

    def test_missing_language_adjustment_skips_market(self):
        bad = _audit("m1", 57)
        bad.pop("language_adjustment")
        llm_pricing._ask = lambda evidence, **_kw: _response([bad])
        result = llm_pricing.price_match(_result(), _evidence(), None, 30.0, force=True)
        self.assertEqual(result.predictions, [])
        self.assertEqual(result.skip_reasons["m1"],
                         "LLM pricing missing language_adjustment object")

    def test_base_final_move_mismatch_skips_market(self):
        bad = _audit("m1", 57, base=55, move=1, direction="up")
        llm_pricing._ask = lambda evidence, **_kw: _response([bad])
        result = llm_pricing.price_match(_result(), _evidence(), None, 30.0, force=True)
        self.assertEqual(result.predictions, [])
        self.assertEqual(result.skip_reasons["m1"],
                         "LLM pricing language_adjustment move does not match final probability")

    def test_direct_odds_move_cap_violation_skips_market(self):
        bad = _audit("m1", 62, base=55, move=7, direction="up")
        llm_pricing._ask = lambda evidence, **_kw: _response([bad])
        result = llm_pricing.price_match(_result(), _evidence(), None, 30.0, force=True)
        self.assertEqual(result.predictions, [])
        self.assertEqual(result.skip_reasons["m1"],
                         "LLM pricing language_adjustment move exceeds 5 point cap")

    def test_direct_odds_move_within_cap_is_accepted(self):
        audit = _audit("m1", 59, base=55, move=4, direction="up")
        llm_pricing._ask = lambda evidence, **_kw: _response([audit])
        result = llm_pricing.price_match(_result(), _evidence(), None, 30.0, force=True)
        self.assertEqual(len(result.predictions), 1)
        self.assertEqual(result.predictions[0].probability_int, 59)

    def test_precollected_online_odds_candidate_must_be_used(self):
        llm_pricing._ask = lambda evidence, **_kw: _response([_audit("m1", 57)])
        result = llm_pricing.price_match(
            _result(), _evidence(with_online_candidate=True),
            None, 30.0, force=True,
        )

        self.assertEqual(result.predictions, [])
        self.assertEqual(
            result.skip_reasons["m1"],
            "LLM pricing ignored pre-collected online odds candidates",
        )

    def test_precollected_online_odds_candidate_in_online_audit_is_accepted(self):
        audit = _audit("m1", 57)
        audit["online_odds_found"] = [{
            "source": "BetOlimp",
            "url": "https://betolimp.co.za/sot",
            "quoted_price_or_odds": "USA (shots on target) (5.5) over 2.08",
            "converted_probability_pct": 44.68,
            "conversion_method": "same-book over/under de-vig",
            "how_used": "direct online price",
        }]
        llm_pricing._ask = lambda evidence, **_kw: _response([audit])
        result = llm_pricing.price_match(
            _result(), _evidence(with_online_candidate=True),
            None, 30.0, force=True,
        )

        self.assertEqual(len(result.predictions), 1)

    def test_leak_guard_refuses_after_kickoff(self):
        calls = []
        llm_pricing._ask = (
            lambda evidence, **_kw: calls.append(1) or _response([_audit("m1", 57)])
        )
        result = llm_pricing.price_match(_result(), _evidence(), None, -1.0, force=True)
        self.assertEqual(calls, [])
        self.assertEqual(result.predictions, [])
        self.assertEqual(result.skip_reasons["m1"], "LLM pricing refused after kickoff")

    def test_refresh_flag_reaches_cache_layer(self):
        seen = []
        llm_pricing._ask = (
            lambda evidence, **kw: seen.append(kw.get("refresh"))
            or _response([_audit("m1", 57)])
        )
        llm_pricing.price_match(
            _result(), _evidence(), None, 30.0, force=True, refresh=True,
        )
        self.assertEqual(seen, [True])


def _response(markets):
    return {
        "briefing": "Home should control territory.",
        "sources": ["https://example.com/preview"],
        "match_read_markdown": "# Match read\n\nHome should control territory.",
        "match_read_sources": ["https://example.com/preview"],
        "subagent_memos": _memos(markets),
        "markets": markets,
    }


def _memos(markets):
    return {
        "base_pricing": [
            {
                "market_id": market["market_id"],
                "base_probability_int": market["base_probability_int"],
                "method": "direct_odds",
                "memo": "Base priced from provided direct odds.",
                "sources": ["provided evidence"],
            }
            for market in markets
        ],
        "match_read_aspects": [
            {
                "aspect": aspect,
                "memo": f"{aspect} reviewed for public audit.",
                "sources": ["https://example.com/preview"],
            }
            for aspect in llm_pricing.MATCH_READ_ASPECTS
        ],
        "question_adjustments": [
            {
                "market_id": market["market_id"],
                "recommended_probability_int": market["probability_int"],
                "memo": "Question adjustment memo supports the final price.",
                "sources": ["https://example.com/preview"],
            }
            for market in markets
        ],
    }


def _audit(market_id, probability, *, base=55, move=2, direction="up"):
    action = "hold" if move == 0 else "move"
    if move == 0:
        direction = "none"
    return {
        "market_id": market_id,
        "base_probability_int": base,
        "probability_int": probability,
        "language_adjustment": {
            "action": action,
            "direction": direction,
            "move_points": move,
            "confidence": "medium",
            "base_used": base,
            "match_read_evidence": [
                {
                    "aspect": "lineups",
                    "source": "provided evidence",
                    "effect": "raises",
                    "why": "strong home XI supports the move",
                }
            ] if move else [],
            "additional_research": [],
            "why_move_or_hold": "match read supports the adjustment.",
        },
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


def _evidence(with_context=False, with_simulator=False, with_online_candidate=False):
    question = {
        "market_id": "m1",
        "direct_odds": [{"probability": 0.55}],
        "related_odds": [],
    }
    if with_online_candidate:
        question["online_odds_candidates"] = [{
            "source": "public-web",
            "bookmaker": "BetOlimp",
            "url": "https://betolimp.co.za/sot",
            "contract": "USA (shots on target) (5.5) over",
            "probability_pct": 44.68,
        }]
    if with_simulator:
        question["simulator_estimate"] = {
            "family": "any_player_threshold",
            "contract_key": "any_player_threshold:goals:>:1:reg",
            "probability_pct": 23.47,
            "calibrated_baseline": {
                "source": "simulator",
                "probability_pct": 23.47,
                "scope": "wc2026",
                "comparison_n": 2277,
                "brier": {
                    "simulator": 0.168899,
                    "empirical_rate": 0.17,
                    "always_50": 0.25,
                },
            },
            "contract_comparison": {
                "wc2026": {
                    "signal": "inconclusive", "n_observations": 2277,
                    "brier": {"simulator": 0.168899, "empirical_rate": 0.17,
                              "always_50": 0.25},
                },
            },
        }
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
