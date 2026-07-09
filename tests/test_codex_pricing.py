import tempfile
import unittest
from pathlib import Path

from bot import codex_pricing
from bot.pipeline import MatchResult


class CodexPricingResponseTests(unittest.TestCase):
    def test_complete_audit_creates_prediction_and_reports(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = _apply(_response([_audit()]), directory=Path(tmp))
            self.assertEqual(result.predictions[0].probability_int, 57)
            self.assertEqual(result.predictions[0].source, "manual-codex")
            self.assertTrue(Path(result.codex_audit_path).is_file())
            self.assertTrue(Path(result.codex_report_path).is_file())
            self.assertTrue(Path(result.codex_match_read_path).is_file())
            report = Path(result.codex_report_path).read_text()
            self.assertIn("# Codex pricing audit", report)
            self.assertIn("language adjustment:", report)

    def test_report_surfaces_context_and_simulator(self):
        evidence = _evidence()
        evidence.update({
            "team_form": {"home": {"games": 3}, "away": {}},
            "player_form": {"home": [{"name": "Player"}], "away": []},
            "referee_profile": {"name": "Ref"},
            "injuries": {"home": [{"player": "X"}], "away": []},
        })
        evidence["question_evidence"][0]["simulator_estimate"] = {
            "family": "any_player_threshold",
            "contract_key": "any_player_threshold:goals:>:1:reg",
            "probability_pct": 23.47,
            "calibrated_baseline": {
                "source": "simulator", "probability_pct": 23.47,
                "scope": "wc2026", "comparison_n": 100,
                "brier": {"simulator": .17, "empirical_rate": .18, "always_50": .25},
            },
        }
        with tempfile.TemporaryDirectory() as tmp:
            result = _apply(_response([_audit()]), evidence=evidence, directory=Path(tmp))
            report = Path(result.codex_report_path).read_text()
        self.assertIn("## Provided context", report)
        self.assertIn("simulator estimate: 23.47%", report)

    def test_missing_required_market_audit_field_is_rejected(self):
        audit = _audit()
        audit.pop("online_odds_found")
        result = _apply(_response([audit]))
        self.assertEqual(result.predictions, [])
        self.assertEqual(
            result.skip_reasons["m1"],
            "Codex pricing missing audit field: online_odds_found",
        )

    def test_missing_match_read_is_rejected(self):
        response = _response([_audit()])
        response.pop("match_read_markdown")
        result = _apply(response)
        self.assertEqual(
            result.skip_reasons["m1"], "Codex pricing missing match_read_markdown",
        )

    def test_move_must_match_base_and_final(self):
        audit = _audit(probability=57, base=55, move=1)
        result = _apply(_response([audit]))
        self.assertIn("does not match final probability", result.skip_reasons["m1"])

    def test_direct_odds_move_cap_is_enforced(self):
        audit = _audit(probability=62, base=55, move=7)
        result = _apply(_response([audit]))
        self.assertIn("exceeds 5 point cap", result.skip_reasons["m1"])

    def test_online_candidate_must_be_audited(self):
        evidence = _evidence()
        evidence["question_evidence"][0]["online_odds_candidates"] = [{
            "bookmaker": "BetOlimp", "url": "https://example.com/price",
            "contract": "Home win",
        }]
        result = _apply(_response([_audit()]), evidence=evidence)
        self.assertIn("pre-collected online odds", result.skip_reasons["m1"])

    def test_boolean_probability_is_rejected(self):
        audit = _audit()
        audit["probability_int"] = True
        result = _apply(_response([audit]))
        self.assertIn("missing numeric", result.skip_reasons["m1"])

    def test_invalid_confidence_is_rejected(self):
        audit = _audit()
        audit["language_adjustment"]["confidence"] = "certain"
        result = _apply(_response([audit]))
        self.assertIn("confidence must be", result.skip_reasons["m1"])

    def test_direct_odds_cannot_be_silently_ignored(self):
        audit = _audit()
        audit["provided_odds_used"] = []
        result = _apply(_response([audit]))
        self.assertIn("without an explicit audit reason", result.skip_reasons["m1"])

    def test_response_schema_is_required(self):
        response = _response([_audit()])
        response.pop("schema_version")
        result = _apply(response)
        self.assertIn("schema_version", result.skip_reasons["m1"])

    def test_response_binding_rejects_wrong_session(self):
        response = _response([_audit()])
        response.update(session_id="other", evidence_hash="abc")
        with self.assertRaisesRegex(ValueError, "session_id"):
            _apply(
                response, require_all_markets=True,
                expected_session_id="expected", expected_evidence_hash="abc",
            )

    def test_response_binding_accepts_exact_session_and_evidence(self):
        response = _response([_audit()])
        response.update(session_id="session", evidence_hash="abc")
        result = _apply(
            response, require_all_markets=True,
            expected_session_id="session", expected_evidence_hash="abc",
        )
        self.assertEqual(result.predictions[0].probability_int, 57)


def _apply(response, *, evidence=None, directory=None, **kwargs):
    if directory is not None:
        return codex_pricing.apply_pricing_response(
            _result(), evidence or _evidence(), None, response,
            directory=directory, model_label="manual-codex", **kwargs,
        )
    with tempfile.TemporaryDirectory() as tmp:
        return codex_pricing.apply_pricing_response(
            _result(), evidence or _evidence(), None, response,
            directory=Path(tmp), model_label="manual-codex", **kwargs,
        )


def _response(markets):
    return {
        "schema_version": 1,
        "briefing": "Home should control territory.",
        "sources": ["https://example.com/preview"],
        "match_read_markdown": "# Match read\n\nHome should control territory.",
        "match_read_sources": ["https://example.com/preview"],
        "subagent_memos": {
            "base_pricing": [{
                "question_id": "Q1",
                "market_id": market["market_id"],
                "base_probability_int": market["base_probability_int"],
                "method": "direct_odds", "memo": "Base from direct odds.",
                "sources": ["provided evidence"],
            } for market in markets],
            "match_read_aspects": [{
                "aspect": aspect, "memo": f"{aspect} reviewed.",
                "sources": ["https://example.com/preview"],
            } for aspect in codex_pricing.MATCH_READ_ASPECTS],
            "question_adjustments": [{
                "question_id": "Q1",
                "market_id": market["market_id"],
                "recommended_probability_int": market["probability_int"],
                "memo": "Adjustment supports the final price.",
                "sources": ["https://example.com/preview"],
            } for market in markets],
        },
        "markets": markets,
    }


def _audit(*, probability=57, base=55, move=2):
    return {
        "question_id": "Q1", "market_id": "m1", "base_probability_int": base,
        "probability_int": probability,
        "language_adjustment": {
            "action": "hold" if move == 0 else "move",
            "direction": "none" if move == 0 else "up",
            "move_points": move, "confidence": "medium", "base_used": base,
            "match_read_evidence": ([{
                "aspect": "lineups", "source": "provided evidence",
                "effect": "raises", "why": "strong home XI",
            }] if move else []),
            "additional_research": [],
            "why_move_or_hold": "The public match read supports this price.",
        },
        "provided_odds_used": [{"source": "api-football", "bookmaker": "Bet365"}],
        "online_odds_found": [],
        "non_odds_factors_used": [{"factor": "lineup", "source": "provided evidence"}],
        "ignored_or_downweighted_evidence": [],
        "reasoning_summary": "Odds and lineup support the submitted probability.",
        "sources": ["https://example.com/preview"],
    }


def _result():
    return MatchResult(
        sp_match={"id": "match", "name": "Home vs Away", "opening_time": "2099-01-01T00:00:00Z"},
        fixture={"fixture": {"id": 42}}, home="Home", away="Away",
        markets=[{"id": "m1", "question": "Will Home win?"}],
    )


def _evidence():
    return {
        "schema_version": 24, "evidence_hash": "abc",
        "match": {"match_id": "match", "home": "Home", "away": "Away",
                  "kickoff": "2099-01-01T00:00:00Z"},
        "question_evidence": [{
            "question_id": "Q1", "market_id": "m1", "question": "Will Home win?",
            "direct_odds": [{"probability": .55}],
            "decision_basis": {"primary": "provided_direct_odds"},
        }],
    }


if __name__ == "__main__":
    unittest.main()
