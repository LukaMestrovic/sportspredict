import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bot.evidence import _compact_simulator_estimate, build_match_evidence, write_evidence
from bot.simulator import model_estimate_kind
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


def _af_first_goal_books():
    return [{
        "name": "Bet365",
        "bets": [{"id": 14, "values": [
            {"value": "Home", "odd": "2.25"},
            {"value": "Draw", "odd": "11.00"},
            {"value": "Away", "odd": "1.73"},
        ]}],
    }]


def _af_cards_compare_books():
    return [{
        "name": "Bet365",
        "bets": [{"id": 158, "values": [
            {"value": "Home", "odd": "5.63"},
            {"value": "Draw", "odd": "3.72"},
            {"value": "Away", "odd": "1.53"},
        ]}],
    }]


def _af_team_score_books():
    return [{
        "name": "Bet365",
        "bets": [{"id": 44, "values": [
            {"value": "Yes", "odd": "1.70"},
            {"value": "No", "odd": "2.20"},
        ]}],
    }]


class _OA:
    def __init__(self):
        self.requested = []

    def event_odds(self, _event_id, markets):
        self.requested.append(tuple(markets))
        return _oa_h2h_books() if markets == ["h2h"] else []


class EvidenceTests(unittest.TestCase):
    def test_direct_mapped_odds_include_provider_bookmaker_and_compact_prices(self):
        result = _result({
            "win": {"market": "match_winner", "subject": "home",
                    "comparator": "win", "threshold": None, "period": "match"},
        })
        ctx = PriceCtx("Home", "Away", _af_h2h_books(), _OA(), {"id": "event"})
        evidence = build_match_evidence(result, ctx, lineups=None, minutes_before=30)

        q = evidence["question_evidence"][0]
        self.assertEqual(
            list(q)[:6],
            [
                "intent", "market_id", "question", "contract_scope",
                "direct_market_spec", "direct_odds",
            ],
        )
        self.assertEqual(q["market_id"], "win")
        self.assertGreaterEqual(len(q["direct_odds"]), 2)
        sources = {obs["source"] for obs in q["direct_odds"]}
        books = {obs["bookmaker"] for obs in q["direct_odds"]}
        self.assertEqual(sources, {"api-football", "odds-api"})
        self.assertIn("Bet365", books)
        self.assertIn("DraftKings", books)
        self.assertTrue(all("probability_pct" in obs for obs in q["direct_odds"]))
        self.assertTrue(all("raw_odds" not in obs for obs in q["direct_odds"]))
        self.assertTrue(all("probability" not in obs for obs in q["direct_odds"]))
        self.assertTrue(all("role" not in obs for obs in q["direct_odds"]))
        self.assertTrue(all("why_relevant" not in obs for obs in q["direct_odds"]))

    def test_simulator_projection_keeps_decisions_not_internal_bookkeeping(self):
        compact = _compact_simulator_estimate({
            "source": "sportspredict-simulator",
            "family": "goal_window",
            "contract_key": "goal_window:before_first_hydration:reg",
            "probability": 0.421,
            "probability_pct": 42.1,
            "explanation": "Learned goal counts and timing.",
            "adjustment_guidance": "Raise for attacking lineups.",
            "model": {"n_sims": 8000, "rate_model": "LearnedRateModel"},
            "note": "context only",
            "historical_evidence": {
                "model_performance": {"all_history": {"brier": 0.16}},
                "empirical_rate": {
                    "all_history": {"available": True, "rate": 0.418, "observations": 3000},
                    "all_history_knockout": {
                        "available": True, "rate": 0.391, "observations": 300,
                    },
                    "wc2026": {"available": True, "rate": 0.44, "matches": 77,
                               "data_through": "2026-06-30"},
                    "wc2026_knockout": {
                        "available": True, "rate": 0.5, "observations": 8,
                    },
                },
                "family_performance": {
                    "family": "goal_window",
                    "all_history": {
                        "available": True, "comparison_signal": "inconclusive",
                        "matches": 2900, "sample_size": {"level": "large"},
                        "brier": {"simulator": 0.157, "empirical_rate": 0.156,
                                  "always_50": 0.25},
                        "coverage": {"fraction": 1.0}, "test_folds": [2021, 2022],
                    },
                    "wc2026": {
                        "available": True, "comparison_signal": "inconclusive_small_sample",
                        "matches": 2, "observations": 4,
                        "sample_size": {"level": "too_small"},
                        "contracts": 2,
                        "coverage": {
                            "labelable_matches": 2, "comparable_matches": 2,
                            "simulator_observations": 4, "comparable_observations": 4,
                        },
                        "brier": {"simulator": 0.1, "empirical_rate": 0.2,
                                  "always_50": 0.25},
                    },
                },
                "contract_performance": {
                    "all_history": {
                        "available": True, "comparison_signal": "simulator_better",
                        "observations": 3000,
                        "brier": {"simulator": 0.17, "empirical_rate": 0.19,
                                  "always_50": 0.25},
                    },
                    "all_history_knockout": {
                        "available": True, "comparison_signal": "inconclusive",
                        "observations": 300,
                        "brier": {"simulator": 0.2, "empirical_rate": 0.21,
                                  "always_50": 0.25},
                    },
                    "wc2026": {
                        "available": True, "comparison_signal": "simulator_better",
                        "matches": 77, "observations": 154,
                        "observation_unit": "team",
                        "sample_size": {"level": "moderate"},
                        "coverage": {
                            "labelable_matches": 77, "comparable_matches": 77,
                            "simulator_observations": 154,
                            "comparable_observations": 154,
                        },
                        "brier": {"simulator": 0.18, "empirical_rate": 0.21,
                                  "always_50": 0.25},
                    },
                    "wc2026_knockout": {
                        "available": True, "comparison_signal": "inconclusive_small_sample",
                        "observations": 8,
                        "brier": {"simulator": 0.22, "empirical_rate": 0.24,
                                  "always_50": 0.25},
                    },
                },
            },
        })

        self.assertNotIn("family", compact)
        self.assertEqual(compact["probability_pct"], 42.1)
        self.assertEqual(compact["empirical_rates"]["all_history"], {
            "rate": 0.418, "n": 3000,
            "population": "All historical labelable observations for this exact contract.",
        })
        self.assertEqual(compact["empirical_rates"]["all_history_knockout"]["n"], 300)
        self.assertEqual(compact["empirical_rates"]["wc2026_knockout"]["rate"], 0.5)
        self.assertNotIn("family_comparison", compact)
        self.assertEqual(compact["contract_comparison"]["all_history"], {
            "basis": "Rolling-origin unseen historical observations for this exact contract.",
            "signal": "simulator_better",
            "n_observations": 3000,
            "brier": {
                "simulator": 0.17, "empirical_rate": 0.19, "always_50": 0.25,
            },
        })
        self.assertEqual(compact["contract_comparison"]["wc2026"], {
            "signal": "simulator_better",
            "basis": (
                "Frozen pre-2026 simulator on every settled WC2026 labelable "
                "observation for this exact contract."
            ),
            "n_observations": 154,
            "brier": {
                "simulator": 0.18, "empirical_rate": 0.21, "always_50": 0.25,
            },
        })
        self.assertEqual(
            set(compact["contract_comparison"]),
            {"all_history", "all_history_knockout", "wc2026", "wc2026_knockout"},
        )
        self.assertEqual(compact["calibrated_baseline"]["source"], "simulator")
        self.assertEqual(compact["calibrated_baseline"]["scope"], "wc2026")
        self.assertEqual(compact["calibrated_baseline"]["probability_pct"], 42.1)
        for redundant in ("source", "model", "note", "historical_evidence", "probability"):
            self.assertNotIn(redundant, compact)

    def test_calibrated_baseline_prefers_empirical_when_exact_brier_is_lower(self):
        compact = _compact_simulator_estimate({
            "probability": 0.5545,
            "probability_pct": 55.45,
            "historical_evidence": {
                "empirical_rate": {
                    "all_history": {
                        "available": True, "rate": 0.525478, "observations": 314,
                    },
                    "all_history_knockout": {
                        "available": True, "rate": 0.488372, "observations": 86,
                    },
                    "wc2026_knockout": {
                        "available": True, "rate": 0.625, "observations": 8,
                    },
                },
                "contract_performance": {
                    "all_history": {
                        "available": True,
                        "comparison_signal": "inconclusive",
                        "observations": 250,
                        "brier": {
                            "simulator": 0.252083,
                            "empirical_rate": 0.243272,
                            "always_50": 0.25,
                        },
                    },
                    "all_history_knockout": {
                        "available": True,
                        "comparison_signal": "empirical_rate_better",
                        "observations": 70,
                        "brier": {
                            "simulator": 0.254286,
                            "empirical_rate": 0.228454,
                            "always_50": 0.25,
                        },
                    },
                    "wc2026_knockout": {
                        "available": True,
                        "comparison_signal": "inconclusive_small_sample",
                        "observations": 8,
                        "brier": {
                            "simulator": 0.3,
                            "empirical_rate": 0.2,
                            "always_50": 0.25,
                        },
                    },
                },
            },
        }, stage="knockout")

        baseline = compact["calibrated_baseline"]
        self.assertEqual(baseline["source"], "empirical_rate")
        self.assertEqual(baseline["scope"], "all_history_knockout")
        self.assertEqual(baseline["probability_pct"], 48.84)
        self.assertEqual(baseline["rate_n"], 86)
        self.assertEqual(baseline["comparison_n"], 70)
        self.assertEqual(baseline["simulator_probability_pct"], 55.45)
        self.assertIn("Smaller current-tournament scope was ignored", baseline["reason"])

    def test_unmapped_question_omits_broad_related_odds(self):
        result = _result({
            "odd": {"market": "none", "subject": "match",
                    "comparator": "yes", "threshold": None, "period": "match"},
        }, question="Will something unusual happen?")
        ctx = PriceCtx("Home", "Away", _af_h2h_books(), _OA(), {"id": "event"})
        evidence = build_match_evidence(result, ctx, lineups=None, minutes_before=30)

        q = evidence["question_evidence"][0]
        self.assertEqual(q["direct_odds"], [])
        self.assertNotIn("related_odds", q)
        self.assertNotIn("deterministic_estimates", q)

    def test_missing_comparison_scopes_are_omitted_not_zero_filled(self):
        compact = _compact_simulator_estimate({
            "probability": 0.42,
            "historical_evidence": {
                "contract_performance": {
                    "all_history": {
                        "available": True,
                        "comparison_signal": "inconclusive",
                        "observations": 100,
                        "brier": {"simulator": 0.2, "always_50": 0.25},
                    },
                },
            },
        })

        self.assertEqual(set(compact["contract_comparison"]), {"all_history"})

    def test_baked_wc2026_scopes_are_omitted_without_live_refresh(self):
        result = _result({
            "odd": {"market": "none", "subject": "match",
                    "comparator": "yes", "threshold": None, "period": "match"},
        }, question="Will there be a goal before the first hydration break?")
        ctx = PriceCtx("Home", "Away", _af_h2h_books(), None, None)
        sim = {
            "contract_key": "goal_window:before_first_hydration:reg",
            "probability": 0.42,
            "historical_evidence": {
                "empirical_rate": {
                    "all_history": {"available": True, "rate": 0.4, "observations": 1000},
                    "wc2026": {"available": True, "rate": 0.55, "observations": 34},
                },
                "contract_performance": {
                    "all_history": {
                        "available": True,
                        "observations": 1000,
                        "brier": {"simulator": 0.2},
                    },
                    "wc2026": {
                        "available": True,
                        "observations": 34,
                        "brier": {"simulator": 0.18},
                    },
                },
            },
        }

        with patch("bot.evidence.simulator.simulator_estimates",
                   return_value={"odd": sim}), patch(
            "bot.evidence.simulator_benchmark.load", return_value={},
        ):
            evidence = build_match_evidence(
                result, ctx, lineups=None, minutes_before=30,
            )

        compact = evidence["question_evidence"][0]["simulator_estimate"]
        self.assertEqual(set(compact["empirical_rates"]), {"all_history"})
        self.assertEqual(set(compact["contract_comparison"]), {"all_history"})

    def test_live_wc2026_refresh_readds_current_scopes(self):
        result = _result({
            "odd": {"market": "none", "subject": "match",
                    "comparator": "yes", "threshold": None, "period": "match"},
        }, question="Will there be a goal before the first hydration break?")
        ctx = PriceCtx("Home", "Away", _af_h2h_books(), None, None)
        sim = {
            "contract_key": "goal_window:before_first_hydration:reg",
            "probability": 0.42,
            "historical_evidence": {
                "empirical_rate": {
                    "all_history": {"available": True, "rate": 0.4, "observations": 1000},
                    "wc2026": {"available": True, "rate": 0.55, "observations": 34},
                },
            },
        }
        snapshot = {
            "contracts": {
                "goal_window:before_first_hydration:reg": {
                    "wc2026": {
                        "available": True,
                        "rate": 0.481013,
                        "observations": 79,
                        "matches": 79,
                    },
                },
            },
        }

        with patch("bot.evidence.simulator.simulator_estimates",
                   return_value={"odd": sim}), patch(
            "bot.evidence.wc2026_evidence.refresh", return_value=snapshot,
        ), patch("bot.evidence.simulator_benchmark.load", return_value={}):
            evidence = build_match_evidence(
                result, ctx, lineups=None, minutes_before=30, af=object(),
            )

        empirical = (
            evidence["question_evidence"][0]["simulator_estimate"]["empirical_rates"]
        )
        self.assertEqual(empirical["wc2026"]["n"], 79)
        self.assertEqual(empirical["wc2026"]["rate"], 0.481013)

    def test_penalty_question_receives_simulator_context(self):
        result = _result({
            "pen": {"market": "none", "subject": "match",
                    "comparator": "yes", "threshold": None, "period": "match"},
        }, question="Will a penalty kick be awarded in the match?")
        ctx = PriceCtx("Home", "Away", _af_h2h_books(), None, None)
        sim = {
            "source": "sportspredict-simulator",
            "model": "LearnedRateModel",
            "probability": 0.241,
            "probability_pct": 24.1,
            "note": "context only",
        }

        with patch("bot.evidence.simulator.simulator_estimates",
                   return_value={"pen": sim}) as estimates:
            evidence = build_match_evidence(result, ctx, lineups=None, minutes_before=30)

        estimates.assert_called_once()
        self.assertEqual(estimates.call_args.kwargs["intents"], result.intents)
        q = evidence["question_evidence"][0]
        self.assertEqual(evidence["schema_version"], 19)
        self.assertEqual(q["simulator_estimate"], {"probability_pct": 24.1})
        self.assertLess(
            list(q).index("direct_odds"),
            list(q).index("simulator_estimate"),
        )
        self.assertNotIn("simulator_model_estimates", q)
        self.assertNotIn("audit_requirement", q)

    def test_sot_question_receives_simulator_context(self):
        result = _result({
            "sot": {"market": "shots_on_target_compare", "subject": "home",
                    "comparator": "more", "threshold": None, "period": "2H"},
        }, question="Will Home have more shots on target than Away in the second half?")
        ctx = PriceCtx("Home", "Away", _af_h2h_books(), None, None)
        sim = {
            "source": "sportspredict-simulator",
            "model": "LearnedRateModel",
            "kind": "team_more_shots_on_target_2h",
            "probability": 0.531,
            "probability_pct": 53.1,
            "note": "context only",
        }

        with patch("bot.evidence.simulator.simulator_estimates",
                   return_value={"sot": sim}) as estimates:
            evidence = build_match_evidence(result, ctx, lineups=None, minutes_before=30)

        estimates.assert_called_once()
        self.assertEqual(
            evidence["question_evidence"][0]["simulator_estimate"],
            {"probability_pct": 53.1},
        )

    def test_non_penalty_question_gets_empty_simulator_context(self):
        result = _result({
            "win": {"market": "match_winner", "subject": "home",
                    "comparator": "win", "threshold": None, "period": "match"},
        })
        ctx = PriceCtx("Home", "Away", _af_h2h_books(), None, None)

        with patch("bot.evidence.simulator.simulator_estimates",
                   return_value={}) as estimates:
            evidence = build_match_evidence(result, ctx, lineups=None, minutes_before=30)

        estimates.assert_called_once()
        self.assertNotIn("simulator_estimate", evidence["question_evidence"][0])

    def test_full_match_first_goal_uses_labeled_regulation_proxy(self):
        result = _result({
            "first": {
                "market": "first_team_to_score", "subject": "home",
                "comparator": "yes", "threshold": None, "period": "match",
                "time_scope": "full_match",
            },
        }, question="Will Home score the first goal of the match?")
        ctx = PriceCtx(
            "Home", "Away", _af_first_goal_books(), None, None, stage="knockout",
        )
        sim = {"contract_key": "first_goal:full:et:team", "probability": 0.45}

        with patch("bot.evidence.simulator.simulator_estimates",
                   return_value={"first": sim}):
            bundle = build_match_evidence(
                result, ctx, lineups=None, minutes_before=30,
            )

        question = bundle["question_evidence"][0]
        self.assertEqual(bundle["schema_version"], 19)
        self.assertEqual(question["direct_market_spec"]["bet_id"], 14)
        self.assertEqual(len(question["direct_odds"]), 1)
        self.assertIn("regulation first-team-to-score proxy",
                      question["direct_odds"][0]["contract_note"])
        self.assertNotIn("simulator_estimate", question)
        self.assertEqual(question["contract_scope"], {
            "time_scope": "full_match",
            "interpretation": "Full match: include extra time if played; exclude shootout events.",
        })

    def test_cards_compare_uses_labeled_yellow_card_proxy(self):
        result = _result({
            "cards": {
                "market": "team_cards", "subject": "away",
                "comparator": "more", "threshold": None, "period": "match",
                "time_scope": "regulation",
            },
        }, question="Will Away receive more cards than Home?")
        ctx = PriceCtx("Home", "Away", _af_cards_compare_books(), None, None)

        with patch("bot.evidence.simulator.simulator_estimates",
                   return_value={"cards": {"probability_pct": 50.0}}):
            bundle = build_match_evidence(
                result, ctx, lineups=None, minutes_before=30,
            )

        question = bundle["question_evidence"][0]
        self.assertEqual(question["direct_market_spec"]["bet_id"], 158)
        self.assertEqual(
            question["direct_market_spec"]["contract_proxy"],
            "yellow_cards_1x2_for_all_cards_compare",
        )
        self.assertEqual(len(question["direct_odds"]), 1)
        direct = question["direct_odds"][0]
        self.assertEqual((direct["bookmaker"], direct["market_key"]), ("Bet365", "af_bet_158"))
        self.assertAlmostEqual(direct["probability_pct"], 59.42, places=2)
        self.assertIn("yellow-card/bookings", direct["contract_note"])
        self.assertNotIn("simulator_estimate", question)

    def test_team_score_excluding_own_goals_uses_labeled_scoreboard_proxy(self):
        result = _result({
            "score": {
                "market": "team_score", "subject": "away",
                "comparator": "yes", "threshold": None, "period": "match",
                "time_scope": "regulation", "excludes_own_goals": True,
            },
        }, question="Will Away score a goal (excluding own goals)?")
        ctx = PriceCtx("Home", "Away", _af_team_score_books(), None, None)

        with patch("bot.evidence.simulator.simulator_estimates",
                   return_value={"score": {"probability_pct": 40.0}}):
            bundle = build_match_evidence(
                result, ctx, lineups=None, minutes_before=30,
            )

        question = bundle["question_evidence"][0]
        self.assertEqual(question["direct_market_spec"]["bet_id"], 44)
        self.assertEqual(
            question["direct_market_spec"]["contract_proxy"],
            "team_to_score_for_team_score_excluding_own_goals",
        )
        self.assertEqual(len(question["direct_odds"]), 1)
        self.assertIn("excluding-own-goals", question["direct_odds"][0]["contract_note"])
        self.assertNotIn("simulator_estimate", question)


class ContextEvidenceTests(unittest.TestCase):
    def test_match_context_blocks_are_embedded_at_top_level(self):
        result = _result({
            "win": {"market": "match_winner", "subject": "home",
                    "comparator": "win", "threshold": None, "period": "match"},
        })
        result.match_context = {
            "team_form": {"home": {"games": 3, "gf_avg": 1.7}, "away": {}},
            "player_form": {"home": [{"name": "Striker One", "sot_per90": 1.2}], "away": []},
            "referee_profile": {"name": "J. Smith", "yellows_per_game": 4.0},
            "injuries": {"home": [{"player": "X", "type": "Out"}], "away": []},
        }
        ctx = PriceCtx("Home", "Away", _af_h2h_books(), None, None)

        with patch("bot.evidence.simulator.simulator_estimates", return_value={}):
            evidence = build_match_evidence(result, ctx, lineups=None, minutes_before=30)

        self.assertEqual(evidence["schema_version"], 19)
        self.assertEqual(
            list(evidence),
            [
                "schema_version",
                "created_at",
                "match",
                "team_form",
                "player_form",
                "referee_profile",
                "injuries",
                "question_evidence",
                "evidence_hash",
            ],
        )
        self.assertEqual(evidence["team_form"]["home"]["gf_avg"], 1.7)
        self.assertEqual(evidence["player_form"]["home"][0]["name"], "Striker One")
        self.assertEqual(evidence["referee_profile"]["yellows_per_game"], 4.0)
        self.assertEqual(evidence["injuries"]["home"][0]["player"], "X")
        for redundant in (
            "questions", "provider_odds_summary", "wc2026_evidence_refresh",
            "live_simulator_benchmark", "llm_research_requirements",
        ):
            self.assertNotIn(redundant, evidence)

    def test_player_market_gets_guidance_instead_of_repeated_form_row(self):
        result = _result({
            "scorer": {"market": "player_goal_scorer", "subject": "player",
                       "player": "Cyle Larin", "comparator": "yes",
                       "threshold": None, "period": "match"},
        }, question="Will Cyle Larin score a goal?")
        result.match_context = {
            "player_form": {
                "home": [{"name": "Cyle Larin", "minutes": 162, "goals": 2,
                          "goals_per90": 1.11}],
                "away": [{"name": "Jonathan David", "minutes": 241, "goals": 3,
                          "goals_per90": 1.12}],
            },
            "player_index": {
                "Cyle Larin": {"name": "Cyle Larin", "minutes": 162, "goals": 2,
                               "goals_per90": 1.11},
                "Jonathan David": {"name": "Jonathan David", "minutes": 241, "goals": 3,
                                   "goals_per90": 1.12},
            },
        }
        ctx = PriceCtx("Home", "Away", _af_h2h_books(), None, None)

        with patch("bot.evidence.simulator.simulator_estimates", return_value={}):
            evidence = build_match_evidence(result, ctx, lineups=None, minutes_before=30)

        q = evidence["question_evidence"][0]
        self.assertNotIn("player_form", q)
        self.assertEqual(evidence["player_form"]["home"][0]["name"], "Cyle Larin")
        self.assertIn("Cyle Larin", q["adjustment_guidance"])
        self.assertIn("goals_per90", q["adjustment_guidance"])
        self.assertIn("direct_odds probability_pct", q["adjustment_guidance"])

    def test_player_shots_market_guidance_uses_sot_metrics(self):
        result = _result({
            "sot": {"market": "player_shots_on_target", "subject": "player",
                    "player": "Cyle Larin", "comparator": "gte",
                    "threshold": 1, "period": "match"},
        }, question="Will Cyle Larin have at least 1 shot on target?")
        ctx = PriceCtx("Home", "Away", _af_h2h_books(), None, None)

        with patch("bot.evidence.simulator.simulator_estimates", return_value={}):
            evidence = build_match_evidence(result, ctx, lineups=None, minutes_before=30)

        guidance = evidence["question_evidence"][0]["adjustment_guidance"]
        self.assertIn("sot_per90", guidance)
        self.assertIn("shots_per90", guidance)

    def test_team_shots_on_target_market_gets_stat_odds_search_guidance(self):
        result = _result({
            "sot": {"market": "team_shots_on_target", "subject": "home",
                    "player": None, "comparator": "gte",
                    "threshold": 6, "period": "match"},
        }, question="Will Home have 6 or more shots on target?")
        ctx = PriceCtx("Home", "Away", _af_h2h_books(), None, None)

        with patch("bot.evidence.simulator.simulator_estimates", return_value={}):
            evidence = build_match_evidence(result, ctx, lineups=None, minutes_before=30)

        guidance = evidence["question_evidence"][0]["adjustment_guidance"]
        self.assertIn("Home shots on target", guidance)
        self.assertIn("line 5.5", guidance)
        self.assertIn("Team Total", guidance)
        self.assertIn("BetOlimp", guidance)
        self.assertIn("de-vig", guidance)

    def test_team_shots_on_target_market_embeds_public_odds_candidates(self):
        candidate = {
            "source": "public-web",
            "bookmaker": "BetOlimp",
            "url": "https://betolimp.co.za/sot",
            "contract": "Home (shots on target) (5.5) over",
            "probability_pct": 44.68,
        }
        result = _result({
            "sot": {"market": "team_shots_on_target", "subject": "home",
                    "player": None, "comparator": "gte",
                    "threshold": 6, "period": "match"},
        }, question="Will Home have 6 or more shots on target?")
        ctx = PriceCtx("Home", "Away", _af_h2h_books(), None, None)

        with patch("bot.evidence.simulator.simulator_estimates", return_value={}), \
                patch("bot.evidence.public_odds.online_odds", return_value=[candidate]):
            evidence = build_match_evidence(result, ctx, lineups=None, minutes_before=30)

        question = evidence["question_evidence"][0]
        self.assertEqual(question["online_odds_candidates"], [candidate])

    def test_second_half_sot_market_gets_period_specific_stat_guidance(self):
        result = _result({
            "sot": {"market": "shots_on_target_compare", "subject": "away",
                    "player": None, "comparator": "more",
                    "threshold": None, "period": "2H"},
        }, question="Will Away have more shots on target than Home in the second half?")
        ctx = PriceCtx("Home", "Away", _af_h2h_books(), None, None)

        with patch("bot.evidence.simulator.simulator_estimates", return_value={}):
            evidence = build_match_evidence(result, ctx, lineups=None, minutes_before=30)

        guidance = evidence["question_evidence"][0]["adjustment_guidance"]
        self.assertIn("second half / 2nd half", guidance)
        self.assertIn("most shots on target", guidance)
        self.assertIn("1x2/most-SOT", guidance)

    def test_player_score_or_assist_market_gets_direct_search_guidance(self):
        result = _result({
            "soa": {"market": "player_score_or_assist", "subject": "player",
                    "player": "Martin Odegaard", "comparator": "yes",
                    "threshold": None, "period": "match"},
        }, question="Will Martin Odegaard score or assist a goal?")
        ctx = PriceCtx("Home", "Away", _af_h2h_books(), None, None)

        with patch("bot.evidence.simulator.simulator_estimates", return_value={}):
            evidence = build_match_evidence(result, ctx, lineups=None, minutes_before=30)

        guidance = evidence["question_evidence"][0]["adjustment_guidance"]
        self.assertIn("score or assist", guidance)
        self.assertIn("player assists", guidance)
        self.assertIn("combine them as a labeled proxy", guidance)

    def test_penalty_red_compound_gets_component_odds_guidance(self):
        result = _result({
            "compound": {"market": "none", "subject": "match", "player": None,
                         "comparator": "yes", "threshold": None, "period": "match"},
        }, question="Will a penalty kick be awarded OR a red card be shown?")
        ctx = PriceCtx("Home", "Away", _af_h2h_books(), None, None)

        with patch("bot.evidence.simulator.simulator_estimates", return_value={}):
            evidence = build_match_evidence(result, ctx, lineups=None, minutes_before=30)

        guidance = evidence["question_evidence"][0]["adjustment_guidance"]
        self.assertIn("Penalty or Red card: yes", guidance)
        self.assertIn("component odds", guidance)
        self.assertIn("penalty awarded", guidance)
        self.assertIn("red card shown", guidance)
        self.assertIn("combine as a union", guidance)

    def test_match_special_questions_get_online_specials_guidance(self):
        cases = [
            (
                "Will a goal be scored before the first hydration break?",
                "Goal scored before the 1st half hydration break",
            ),
            (
                "Will a substitute score a goal in regulation?",
                "Substitute to come on and score",
            ),
            (
                "Will any player record 2 or more shots on target?",
                "any player 2+ shots on target",
            ),
        ]
        for question, expected in cases:
            result = _result({
                "special": {"market": "none", "subject": "match", "player": None,
                            "comparator": "yes", "threshold": None, "period": "match"},
            }, question=question)
            ctx = PriceCtx("Home", "Away", _af_h2h_books(), None, None)

            with patch("bot.evidence.simulator.simulator_estimates", return_value={}):
                evidence = build_match_evidence(result, ctx, lineups=None, minutes_before=30)

            self.assertIn(expected, evidence["question_evidence"][0]["adjustment_guidance"])

    def test_btts_and_goals_compound_gets_combined_market_guidance(self):
        result = _result({
            "compound": {"market": "none", "subject": "match", "player": None,
                         "comparator": "yes", "threshold": None, "period": "match"},
        }, question="Will both teams score AND the match have 3 or more total goals?")
        ctx = PriceCtx("Home", "Away", _af_h2h_books(), None, None)

        with patch("bot.evidence.simulator.simulator_estimates", return_value={}):
            evidence = build_match_evidence(result, ctx, lineups=None, minutes_before=30)

        guidance = evidence["question_evidence"][0]["adjustment_guidance"]
        self.assertIn("BTTS & Over 2.5", guidance)
        self.assertIn("exact combined odds", guidance)

    def test_non_player_market_has_no_player_form_key(self):
        result = _result({
            "win": {"market": "match_winner", "subject": "home", "player": None,
                    "comparator": "win", "threshold": None, "period": "match"},
        })
        result.match_context = {"player_index": {"X": {"name": "X"}}}
        ctx = PriceCtx("Home", "Away", _af_h2h_books(), None, None)

        with patch("bot.evidence.simulator.simulator_estimates", return_value={}):
            evidence = build_match_evidence(result, ctx, lineups=None, minutes_before=30)

        self.assertNotIn("player_form", evidence["question_evidence"][0])
        self.assertNotIn("adjustment_guidance", evidence["question_evidence"][0])

    def test_missing_context_yields_empty_blocks(self):
        result = _result({
            "win": {"market": "match_winner", "subject": "home",
                    "comparator": "win", "threshold": None, "period": "match"},
        })  # no match_context attached
        ctx = PriceCtx("Home", "Away", _af_h2h_books(), None, None)

        with patch("bot.evidence.simulator.simulator_estimates", return_value={}):
            evidence = build_match_evidence(result, ctx, lineups=None, minutes_before=30)

        self.assertEqual(evidence["team_form"], {})
        self.assertEqual(evidence["player_form"], {})
        self.assertEqual(evidence["referee_profile"], {})
        self.assertEqual(evidence["injuries"], {})

    def test_write_evidence_preserves_question_evidence_key_order(self):
        result = _result({
            "win": {"market": "match_winner", "subject": "home",
                    "comparator": "win", "threshold": None, "period": "match"},
        })
        ctx = PriceCtx("Home", "Away", _af_h2h_books(), None, None)

        with patch("bot.evidence.simulator.simulator_estimates", return_value={}):
            evidence = build_match_evidence(result, ctx, lineups=None, minutes_before=30)

        with tempfile.TemporaryDirectory() as tmp:
            path = write_evidence(evidence, directory=Path(tmp))
            text = path.read_text()

        self.assertLess(text.index('"intent"'), text.index('"market_id"'))
        self.assertLess(text.index('"market_id"'), text.index('"question"'))
        self.assertLess(text.index('"question"'), text.index('"contract_scope"'))
        self.assertLess(text.index('"contract_scope"'), text.index('"direct_odds"'))
        self.assertLess(text.index('"match"'), text.index('"team_form"'))
        self.assertLess(text.index('"team_form"'), text.index('"player_form"'))
        self.assertLess(text.index('"player_form"'), text.index('"question_evidence"'))
        self.assertLess(text.index('"question_evidence"'), text.index('"evidence_hash"'))


class SimulatorModelTests(unittest.TestCase):
    def test_penalty_market_kind_is_limited_to_requested_wordings(self):
        self.assertEqual(
            model_estimate_kind("Will a penalty kick be awarded in the match?"),
            "penalty_awarded",
        )
        self.assertEqual(
            model_estimate_kind(
                "Will a penalty kick be awarded OR a red card be shown in the match?"
            ),
            "penalty_or_red",
        )
        self.assertIsNone(model_estimate_kind("Will there be a red card?"))

    def test_sot_market_kind_is_limited_to_requested_wordings(self):
        self.assertEqual(
            model_estimate_kind(
                "Will Home have more shots on target than Away in the second half?",
                {"market": "shots_on_target_compare", "subject": "home",
                 "comparator": "more", "period": "2H"},
            ),
            "team_more_shots_on_target_2h",
        )
        self.assertEqual(
            model_estimate_kind(
                "Will Home have 6 or more shots on target?",
                {"market": "team_shots_on_target", "subject": "home",
                 "comparator": "gte", "threshold": 6, "period": "match"},
            ),
            "team_shots_on_target_threshold",
        )
        self.assertEqual(
            model_estimate_kind(
                "At halftime, will both teams have at least 1 shot on target?",
                {"market": "none", "subject": "match",
                 "comparator": "yes", "threshold": None, "period": "1H"},
            ),
            "both_teams_shot_on_target_1h",
        )
        self.assertIsNone(
            model_estimate_kind(
                "Will Marcel Sabitzer have at least 1 shot on target?",
                {"market": "player_shots_on_target", "subject": "player",
                 "comparator": "gte", "threshold": 1, "period": "match"},
            )
        )


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
