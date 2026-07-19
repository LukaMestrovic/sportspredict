import math
import statistics
import unittest

from bot.live_odds_proxy import (
    HELPER_ID_PREFIX,
    MAX_ABS_HELPER_LOGIT_GAP,
    build_proxy_and_blend,
    helper_targets,
)
from bot.matcher import match_intent
from bot.odds_context import PriceCtx


HOME = "Spain"
AWAY = "Argentina"


def _intent(
    market,
    subject="match",
    comparator="yes",
    threshold=None,
    period="match",
    player=None,
    time_scope="regulation",
    excludes_own_goals=False,
):
    return {
        "market": market,
        "subject": subject,
        "player": player,
        "comparator": comparator,
        "threshold": threshold,
        "period": period,
        "time_scope": time_scope,
        "excludes_own_goals": excludes_own_goals,
    }


def _book(name, bet_id, values):
    return {
        "name": name,
        "bets": [{"id": bet_id, "values": values}],
    }


def _ou(over, under, line):
    return [
        {"value": f"Over {line}", "odd": str(over)},
        {"value": f"Under {line}", "odd": str(under)},
    ]


class _ForbiddenOddsAPI:
    def __getattr__(self, name):
        raise AssertionError(f"live-odds proxy must not call Odds API: {name}")


class HelperTargetTests(unittest.TestCase):
    def test_allowlisted_helpers_map_to_disclosed_catalogue_contracts(self):
        cases = [
            (
                "joint_sot",
                "Will each team record 4 or more shots on target in regulation?",
                _intent("none"),
                87,
            ),
            (
                "legacy_final_winner",
                "Will Spain win the third-place match (Bronze Final)?",
                _intent(
                    "match_winner", "home", "win",
                    time_scope="full_match",
                ),
                61,
            ),
            (
                "cards_contract_proxy",
                "Will Spain receive more cards than Argentina in regulation?",
                _intent("team_cards", "home", "more"),
                158,
            ),
            (
                "team_score_contract_proxy",
                "Will Spain score a goal excluding own goals in regulation?",
                _intent(
                    "team_score", "home", excludes_own_goals=True,
                ),
                43,
            ),
            (
                "first_goal_scope_proxy",
                "Will Spain score the first goal including extra time?",
                _intent(
                    "first_team_to_score", "home",
                    time_scope="full_match",
                ),
                14,
            ),
            (
                "score_assist",
                "Will Lamine Yamal (Spain, #19) score or assist a goal in regulation?",
                _intent(
                    "player_score_or_assist", "player", player="Lamine Yamal",
                ),
                92,
            ),
            (
                "brace",
                "Will any player score 2 or more goals in regulation?",
                _intent("none"),
                5,
            ),
            (
                "substitute",
                "Will a substitute score or assist a goal in regulation?",
                _intent("substitute_score_or_assist"),
                5,
            ),
            (
                "hydration",
                "Will a goal be scored before the first hydration break?",
                _intent("goal_window"),
                6,
            ),
            (
                "before_second_hydration",
                "Will a goal be scored before the second hydration break?",
                _intent("goal_window"),
                5,
            ),
            (
                "after_second_hydration",
                "Will a goal be scored after the second hydration break?",
                _intent("goal_window"),
                26,
            ),
            (
                "first_goal_second_half",
                "Will the first goal be scored in the second half?",
                _intent("first_goal_half", period="2H"),
                26,
            ),
            (
                "stoppage_card",
                "Will a card be shown in stoppage time?",
                _intent("card_stoppage"),
                80,
            ),
            (
                "lead",
                "Will Argentina lead at any time in regulation?",
                _intent("lead_any_time", "away"),
                14,
            ),
            (
                "team_player_sot",
                "Will any Spain player record 2 or more shots on target?",
                _intent(
                    "any_team_player_shots_on_target", "home", "gte", 2,
                ),
                88,
            ),
            (
                "assisted_first_goal",
                "Will the first goal of the match be credited with an assist?",
                _intent("first_goal_assisted"),
                5,
            ),
            (
                "two_same_half",
                "Will either team score 2 or more goals in the same half?",
                _intent("team_two_plus_same_half", comparator="gte", threshold=2),
                5,
            ),
            (
                "penalty_scored",
                "Will a penalty kick be scored in regulation?",
                _intent("penalty_scored"),
                163,
            ),
            (
                "player_compare",
                "Will Lamine Yamal (Spain, #19) record more shots on target than "
                "Lionel Messi (Argentina, #10) in regulation?",
                _intent(
                    "player_sot_compare", "player", "more",
                    player="Lamine Yamal vs Lionel Messi",
                ),
                176,
            ),
            (
                "unique_shooters",
                "Will 5 or more different Spain players attempt a shot?",
                _intent(
                    "team_unique_shooters", "home", "gte", "5",
                ),
                88,
            ),
        ]
        markets = [
            {"id": market_id, "question": question}
            for market_id, question, _intent_value, _bet_id in cases
        ]
        intents = {
            market_id: intent
            for market_id, _question, intent, _bet_id in cases
        }

        helpers = helper_targets(markets, intents, HOME, AWAY)

        self.assertEqual(set(helpers), {case[0] for case in cases})
        for market_id, _question, _intent_value, expected_bet_id in cases:
            with self.subTest(market_id=market_id):
                group = helpers[market_id]
                self.assertTrue(
                    group["market"]["id"].startswith(
                        f"{HELPER_ID_PREFIX}:{market_id}:"
                    )
                )
                self.assertTrue(group["market"]["question"])
                self.assertTrue(group["recipe_id"])
                spec = match_intent(
                    group["intent"], HOME, AWAY, stage="knockout",
                )
                self.assertIsNotNone(spec)
                self.assertEqual(spec["bet_id"], expected_bet_id)

        self.assertEqual(
            helpers["legacy_final_winner"]["market"]["question"],
            "Will Spain advance?",
        )
        self.assertEqual(
            helpers["cards_contract_proxy"]["market"]["question"],
            "Will Spain receive more yellow cards than Argentina in regulation?",
        )
        self.assertFalse(
            helpers["team_score_contract_proxy"]["intent"][
                "excludes_own_goals"
            ]
        )
        self.assertEqual(
            helpers["team_score_contract_proxy"]["market"]["question"],
            "Will Spain score at least 1 goal in regulation?",
        )
        self.assertEqual(
            helpers["first_goal_scope_proxy"]["intent"]["time_scope"],
            "regulation",
        )

    def test_player_comparison_requires_explicit_first_player_team(self):
        intent = _intent(
            "player_sot_compare", "player", "more",
            player="Lamine Yamal vs Lionel Messi",
        )
        markets = [
            {
                "id": "missing",
                "question": (
                    "Will Lamine Yamal record more shots on target than Lionel Messi?"
                ),
            },
            {
                "id": "wrong_team",
                "question": (
                    "Will Lamine Yamal (Brazil, #19) record more shots on target "
                    "than Lionel Messi (Argentina, #10)?"
                ),
            },
            {
                "id": "alias",
                "question": (
                    "Will Lamine Yamal (ESP, #19) record more shots on target than "
                    "Lionel Messi (ARG, #10)?"
                ),
            },
        ]

        helpers = helper_targets(
            markets, {market["id"]: intent for market in markets}, HOME, AWAY,
        )

        self.assertNotIn("missing", helpers)
        self.assertNotIn("wrong_team", helpers)
        self.assertEqual(helpers["alias"]["intent"]["subject"], "home")
        self.assertEqual(
            helpers["alias"]["market"]["question"],
            "Will Spain record more shots on target than Argentina in regulation?",
        )
        self.assertEqual(
            match_intent(helpers["alias"]["intent"], HOME, AWAY)["bet_id"],
            176,
        )

    def test_unknown_and_invalid_thresholds_are_not_enrolled(self):
        markets = [
            {"id": "unknown", "question": "Will the coin toss be heads?"},
            {"id": "fraction", "question": "Will Spain use many shooters?"},
        ]
        intents = {
            "unknown": _intent("none"),
            "fraction": _intent(
                "team_unique_shooters", "home", "gte", 4.5,
            ),
        }
        self.assertEqual(helper_targets(markets, intents, HOME, AWAY), {})


class ProxyBlendTests(unittest.TestCase):
    def setUp(self):
        self.market = {
            "id": "joint_sot",
            "question": (
                "Will each team record 4 or more shots on target in regulation?"
            ),
        }
        self.intent = _intent("none")

    def _helper_id(self, market=None, intent=None):
        market = market or self.market
        intent = intent or self.intent
        helper = helper_targets(
            [market], {market["id"]: intent}, HOME, AWAY,
        )[market["id"]]
        return helper["market"]["id"]

    def test_blend_uses_calibrated_target_center_and_retains_each_book(self):
        books = [
            _book("Book A", 87, _ou(1.80, 2.00, 7.5)),
            _book("Book B", 87, _ou(2.20, 1.70, 7.5)),
        ]
        ctx = PriceCtx(
            HOME, AWAY, books, _ForbiddenOddsAPI(), {"id": "unused"},
            stage="knockout",
        )
        target_estimate = {
            "probability_pct": 20.0,
            "calibrated_baseline": {
                "source": "exact_contract_empirical",
                "probability_pct": 34.0,
            },
        }
        helper_id = self._helper_id()
        helper_estimates = {
            helper_id: {
                "probability_pct": 50.0,
                "contract_key": "total_shots_on_target:>=:8:reg",
            },
        }

        proxy, baseline = build_proxy_and_blend(
            self.market, self.market["question"], self.intent,
            target_estimate, helper_estimates, ctx,
        )

        self.assertIsNotNone(proxy)
        self.assertIsNotNone(baseline)
        self.assertFalse(proxy["is_direct_odds"])
        self.assertFalse(proxy["target_contract_match"])
        self.assertTrue(proxy["no_marginal_relabel"])
        self.assertIn("not the SportPredict target", proxy["warning"])
        self.assertEqual(proxy["helper_contract"]["provider_spec"]["bet_id"], 87)
        self.assertEqual(proxy["helper_contract"]["market_id"], helper_id)
        self.assertEqual(proxy["helper_contract"]["simulator_probability_pct"], 50.0)
        self.assertEqual(
            proxy["helper_contract"]["simulator_contract_key"],
            "total_shots_on_target:>=:8:reg",
        )
        self.assertEqual(len(proxy["observations"]), 2)
        self.assertEqual(
            [item["bookmaker"] for item in proxy["observations"]],
            ["Book A", "Book B"],
        )
        for observation in proxy["observations"]:
            self.assertFalse(observation["is_direct_for_target"])
            self.assertFalse(observation["target_contract_match"])
            self.assertEqual(
                observation["devig_method"],
                "same-book over/under de-vig",
            )
            self.assertEqual(len(observation["raw_odds"]), 2)

        self.assertEqual(baseline["target_simulator_center_pct"], 34.0)
        self.assertEqual(
            baseline["target_simulator_center_source"],
            "calibrated_baseline:exact_contract_empirical",
        )
        self.assertEqual(baseline["book_count"], 2)
        self.assertFalse(baseline["is_direct_odds"])
        self.assertIn("clip(logit(p_live_helper_book)", baseline["formula"])

        per_book = baseline["per_book_estimates"]
        expected_central = statistics.median(
            item["blended_target_probability_pct"] for item in per_book
        )
        self.assertAlmostEqual(baseline["probability_pct"], expected_central)
        self.assertEqual(
            baseline["probability_pct_range"],
            {
                "min": min(
                    item["blended_target_probability_pct"] for item in per_book
                ),
                "max": max(
                    item["blended_target_probability_pct"] for item in per_book
                ),
            },
        )

        live_a = (1.0 / 1.80) / ((1.0 / 1.80) + (1.0 / 2.00))
        raw_gap = math.log(live_a / (1.0 - live_a))
        expected_a = 1.0 / (
            1.0 + math.exp(-(
                math.log(0.34 / 0.66) + 0.45 * raw_gap
            ))
        )
        self.assertAlmostEqual(
            per_book[0]["blended_target_probability_pct"],
            expected_a * 100.0,
            places=2,
        )

    def test_helper_logit_residual_is_capped_before_transfer(self):
        books = [_book("Extreme", 87, _ou(1.001, 1000.0, 7.5))]
        ctx = PriceCtx(HOME, AWAY, books, None, None)
        helper_id = self._helper_id()

        _proxy, baseline = build_proxy_and_blend(
            self.market, self.market["question"], self.intent,
            {"probability_pct": 40.0},
            {helper_id: {"probability_pct": 5.0}},
            ctx,
        )

        self.assertIsNotNone(baseline)
        estimate = baseline["per_book_estimates"][0]
        self.assertGreater(
            estimate["raw_helper_logit_gap"], MAX_ABS_HELPER_LOGIT_GAP,
        )
        self.assertEqual(
            estimate["capped_helper_logit_gap"],
            MAX_ABS_HELPER_LOGIT_GAP,
        )
        self.assertEqual(
            estimate["applied_target_logit_adjustment"],
            round(0.45 * MAX_ABS_HELPER_LOGIT_GAP, 6),
        )

    def test_player_component_remains_single_sided_and_non_direct(self):
        market = {
            "id": "yamal_involvement",
            "question": "Will Lamine Yamal score or assist in regulation?",
        }
        intent = _intent(
            "player_score_or_assist", "player", player="Lamine Yamal",
        )
        helper_id = self._helper_id(market, intent)
        ctx = PriceCtx(
            HOME,
            AWAY,
            [_book("Player Book", 92, [
                {"value": "L. Yamal", "odd": "2.00"},
                {"value": "Lionel Messi", "odd": "2.20"},
            ])],
            _ForbiddenOddsAPI(),
            {"id": "unused"},
        )

        proxy, baseline = build_proxy_and_blend(
            market, market["question"], intent,
            {"probability_pct": 45.0},
            {helper_id: {"probability_pct": 30.0}},
            ctx,
        )

        self.assertIsNotNone(proxy)
        self.assertIsNotNone(baseline)
        self.assertEqual(len(proxy["observations"]), 1)
        self.assertEqual(
            proxy["observations"][0]["devig_method"],
            "single-sided player prop haircut",
        )
        self.assertFalse(proxy["observations"][0]["target_contract_match"])
        self.assertGreater(baseline["probability_pct"], 45.0)

    def test_blend_fails_closed_when_any_required_component_is_missing(self):
        valid_ctx = PriceCtx(
            HOME,
            AWAY,
            [_book("Book", 87, _ou(1.90, 1.90, 7.5))],
            None,
            None,
        )
        no_line_ctx = PriceCtx(
            HOME,
            AWAY,
            [_book("Book", 87, _ou(1.90, 1.90, 8.5))],
            None,
            None,
        )
        helper_id = self._helper_id()
        valid_target = {"probability_pct": 40.0}
        valid_helpers = {helper_id: {"probability_pct": 50.0}}

        cases = [
            (None, valid_helpers, valid_ctx),
            ({"probability_pct": 0.0}, valid_helpers, valid_ctx),
            (valid_target, {}, valid_ctx),
            (valid_target, {helper_id: {"probability_pct": 100.0}}, valid_ctx),
            (valid_target, valid_helpers, no_line_ctx),
        ]
        for target, helpers, ctx in cases:
            with self.subTest(target=target, helpers=helpers, books=ctx.af_books):
                self.assertEqual(
                    build_proxy_and_blend(
                        self.market, self.market["question"], self.intent,
                        target, helpers, ctx,
                    ),
                    (None, None),
                )

    def test_new_penalty_recipe_transfers_same_book_award_residual(self):
        market = {
            "id": "penalty_scored",
            "question": "Will a penalty kick be scored in regulation?",
        }
        intent = _intent("penalty_scored")
        helper_id = self._helper_id(market, intent)
        ctx = PriceCtx(
            HOME,
            AWAY,
            [_book("Book", 163, [
                {"value": "Yes", "odd": "3.00"},
                {"value": "No", "odd": "1.35"},
            ])],
            None,
            None,
        )

        proxy, baseline = build_proxy_and_blend(
            market, market["question"], intent,
            {"probability_pct": 18.0},
            {helper_id: {"probability_pct": 20.0}},
            ctx,
        )

        self.assertIsNotNone(proxy)
        self.assertIsNotNone(baseline)
        self.assertEqual(proxy["helper_contract"]["provider_spec"]["bet_id"], 163)
        self.assertEqual(
            proxy["observations"][0]["devig_method"],
            "same-book categorical de-vig",
        )
        self.assertGreater(baseline["probability_pct"], 18.0)


if __name__ == "__main__":
    unittest.main()
