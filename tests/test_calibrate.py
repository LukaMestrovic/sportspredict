"""Unit tests for the LLM calibration layer's deterministic tilt mapper.

Pure stdlib, no network: the LLM call (``calibrate._ask``) is monkeypatched with
canned responses so we test only the math and the guardrails. Run with:

    python -m unittest discover -s tests
    python tests/test_calibrate.py
"""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot import calibrate            # noqa: E402
from bot.pipeline import MatchResult, Prediction  # noqa: E402


def _prediction(prob_int: int, n_books: int, book_probs: list[float]) -> Prediction:
    return Prediction(
        market_id=f"m{prob_int}-{n_books}",
        question="Will X happen?",
        probability=prob_int / 100.0,
        probability_int=prob_int,
        n_books=n_books,
        market_label="test",
        source="api-football" if n_books else "empirical",
        book_probabilities=list(book_probs),
    )


def _result(preds: list[Prediction]) -> MatchResult:
    return MatchResult(
        sp_match={"id": "match1", "opening_time": "2026-06-23T18:00:00Z",
                  "name": "A vs B"},
        fixture=None, home="A", away="B", predictions=preds,
    )


def _canned(tilts):
    """Return a fake _ask that yields the given {market_id: tilt_points}."""
    def fake_ask(briefing):
        return {"briefing": "test", "sources": [],
                "tilts": [{"market_id": mid, "tilt_points": tp, "rationale": "r"}
                          for mid, tp in tilts.items()]}
    return fake_ask


class CalibrateTest(unittest.TestCase):
    def setUp(self):
        # _ask is patched in each test, so no real key/network is used; but the
        # guard requires a key to be present.
        calibrate.config.OPENAI_API_KEY = "test-key"
        self._orig_ask = calibrate._ask

    def tearDown(self):
        calibrate._ask = self._orig_ask

    def _run(self, preds, tilts, minutes_before=30.0):
        calibrate._ask = _canned(tilts)
        result = _result(preds)
        return calibrate.calibrate(result, lineups=None,
                                   minutes_before=minutes_before, force=True)

    def test_cap_for_books(self):
        self.assertEqual(calibrate.cap_for_books(0), 18)   # empirical
        self.assertEqual(calibrate.cap_for_books(1), 45)   # lone book
        self.assertEqual(calibrate.cap_for_books(3), 18)   # thin
        self.assertEqual(calibrate.cap_for_books(6), 8)
        self.assertEqual(calibrate.cap_for_books(9), 6)    # liquid consensus

    def test_lone_book_benched_player_collapses(self):
        # The Gonçalo Ramos case: one stale book at 51% for a benched player.
        pred = _prediction(51, n_books=1, book_probs=[0.51])
        self._run([pred], {pred.market_id: -48})
        self.assertLessEqual(pred.probability_int, 20)  # large correction off a lone book
        self.assertGreaterEqual(pred.probability_int, 1)
        self.assertEqual(pred.anchor_probability_int, 51)
        self.assertEqual(pred.applied_delta, pred.probability_int - 51)

    def test_liquid_market_move_is_capped(self):
        # A deep consensus must barely move, even on an extreme LLM tilt.
        pred = _prediction(55, n_books=9, book_probs=[0.55] * 9)
        self._run([pred], {pred.market_id: -50})
        self.assertLessEqual(abs(pred.probability_int - 55), 6)  # cap = 6 pts

    def test_empirical_anchor_uses_weight_and_cap(self):
        pred = _prediction(30, n_books=0, book_probs=[])
        self._run([pred], {pred.market_id: +30})
        self.assertGreater(pred.probability_int, 30)
        self.assertLessEqual(pred.probability_int, 48)  # cap = 18 pts above 30

    def test_no_tilt_leaves_anchor_unchanged(self):
        pred = _prediction(60, n_books=5, book_probs=[0.6] * 5)
        self._run([pred], {})  # LLM returns no tilt for this market
        self.assertEqual(pred.probability_int, 60)
        self.assertEqual(pred.anchor_probability_int, 60)
        self.assertEqual(pred.applied_delta, 0)

    def test_malformed_response_is_safe_noop(self):
        def boom(briefing):
            raise RuntimeError("bad json")
        calibrate._ask = boom
        pred = _prediction(40, n_books=3, book_probs=[0.39, 0.41, 0.40])
        result = calibrate.calibrate(_result([pred]), lineups=None,
                                     minutes_before=30.0, force=True)
        self.assertEqual(result.predictions[0].probability_int, 40)  # unchanged

    def test_leak_guard_refuses_after_kickoff(self):
        calls = []
        calibrate._ask = lambda briefing: calls.append(1) or _canned({})(briefing)
        pred = _prediction(40, n_books=3, book_probs=[0.39, 0.41, 0.40])
        calibrate.calibrate(_result([pred]), lineups=None,
                            minutes_before=-5.0, force=True)
        self.assertEqual(calls, [])                  # never researched
        self.assertEqual(pred.probability_int, 40)   # unchanged

    def test_output_always_in_range(self):
        pred = _prediction(2, n_books=1, book_probs=[0.02])
        self._run([pred], {pred.market_id: -50})
        self.assertGreaterEqual(pred.probability_int, 1)
        self.assertLessEqual(pred.probability_int, 99)


class BriefingTest(unittest.TestCase):
    def test_tier_labels_track_book_count(self):
        self.assertEqual(calibrate._tier_for_books(9), "deep-liquid")
        self.assertEqual(calibrate._tier_for_books(3), "thin")
        self.assertEqual(calibrate._tier_for_books(1), "thin")
        self.assertEqual(calibrate._tier_for_books(0), "no-market")

    def test_briefing_carries_referee_venue_tier_and_cap(self):
        preds = [_prediction(55, 9, [0.55] * 9), _prediction(40, 0, [])]
        result = MatchResult(
            sp_match={"id": "m1", "opening_time": "2026-06-23T18:00:00Z",
                      "name": "A vs B"},
            fixture={"fixture": {"id": 7, "referee": "P. Sampaio, Brazil",
                                 "venue": {"name": "Estadio Azteca",
                                           "city": "Mexico City"}}},
            home="A", away="B", predictions=preds,
        )
        b = calibrate.build_briefing(result, lineups=None, minutes_before=30.0)
        self.assertEqual(b["referee"], "P. Sampaio, Brazil")
        self.assertEqual(b["venue"], "Estadio Azteca, Mexico City")
        q_deep, q_nomarket = b["questions"]
        self.assertEqual(q_deep["tier"], "deep-liquid")
        self.assertEqual(q_deep["max_move"], calibrate.cap_for_books(9))
        self.assertEqual(q_nomarket["tier"], "no-market")
        self.assertEqual(q_nomarket["max_move"], calibrate.cap_for_books(0))

    def test_briefing_tolerates_missing_fixture(self):
        result = MatchResult(
            sp_match={"id": "m1", "opening_time": "2026-06-23T18:00:00Z",
                      "name": "A vs B"},
            fixture=None, home="A", away="B",
            predictions=[_prediction(50, 3, [0.49, 0.5, 0.51])],
        )
        b = calibrate.build_briefing(result, lineups=None, minutes_before=30.0)
        self.assertIsNone(b["referee"])
        self.assertIsNone(b["venue"])


class PromptTemplateTest(unittest.TestCase):
    def test_designed_template_loads_with_the_new_sources(self):
        calibrate._prompt_cache = None  # force a fresh disk read
        text = calibrate._load_prompt()
        self.assertGreater(len(text), 1000)
        for token in ("Pinnacle", "Polymarket", "Kalshi", "Weather", "max_move"):
            self.assertIn(token, text)

    def test_cache_key_changes_when_template_changes(self):
        try:
            calibrate._prompt_cache = "PROMPT A"
            key_a = calibrate._cache_key("m1")
            self.assertEqual(key_a, calibrate._cache_key("m1"))   # stable for same inputs
            calibrate._prompt_cache = "PROMPT B"
            self.assertNotEqual(key_a, calibrate._cache_key("m1"))  # template edit re-keys
        finally:
            calibrate._prompt_cache = None


if __name__ == "__main__":
    unittest.main()
