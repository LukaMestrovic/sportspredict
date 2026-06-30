import unittest
from unittest.mock import patch

from bot.evidence import build_match_evidence
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
        self.assertEqual(evidence["schema_version"], 7)
        self.assertEqual(q["simulator_model_estimates"], [sim])
        self.assertIn("fallback simulator context", q["audit_requirement"])

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
        self.assertEqual(evidence["question_evidence"][0]["simulator_model_estimates"], [sim])

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
        self.assertEqual(evidence["question_evidence"][0]["simulator_model_estimates"], [])

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
        self.assertEqual(bundle["schema_version"], 7)
        self.assertEqual(question["direct_market_spec"]["bet_id"], 14)
        self.assertEqual(len(question["direct_odds"]), 1)
        self.assertIn("regulation first-team-to-score proxy",
                      question["direct_odds"][0]["why_relevant"])
        self.assertEqual(question["simulator_model_estimates"], [])
        self.assertEqual(question["contract_scope"], {
            "time_scope": "full_match",
            "interpretation": "Full match: include extra time if played; exclude shootout events.",
        })


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

        self.assertEqual(evidence["schema_version"], 7)
        self.assertEqual(evidence["team_form"]["home"]["gf_avg"], 1.7)
        self.assertEqual(evidence["player_form"]["home"][0]["name"], "Striker One")
        self.assertEqual(evidence["referee_profile"]["yellows_per_game"], 4.0)
        self.assertEqual(evidence["injuries"]["home"][0]["player"], "X")

    def test_player_market_gets_that_players_exact_row(self):
        result = _result({
            "scorer": {"market": "player_goal_scorer", "subject": "player",
                       "player": "Cyle Larin", "comparator": "yes",
                       "threshold": None, "period": "match"},
        }, question="Will Cyle Larin score a goal?")
        result.match_context = {
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
        # The attached row is Larin's, never David's.
        self.assertEqual(q["player_form"]["name"], "Cyle Larin")
        self.assertEqual(q["player_form"]["goals"], 2)

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
