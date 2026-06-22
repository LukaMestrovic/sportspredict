import unittest

from bot.derive import (
    SHOT_RATE_DEBIAS, _infer_total_rate, _lambda_for_tail, _poisson_tail,
    _shot_model, price_empirical,
)
from bot.pricing import PriceCtx


class PoissonModelTests(unittest.TestCase):
    def test_tail_inversion_round_trips(self):
        for threshold, probability in ((1, 0.4), (4, 0.55), (8, 0.35)):
            rate = _lambda_for_tail(threshold, probability)
            self.assertAlmostEqual(_poisson_tail(rate, threshold), probability, places=7)

    def test_higher_rate_has_higher_tail_probability(self):
        self.assertGreater(_poisson_tail(8, 6), _poisson_tail(5, 6))

    def test_shot_model_applies_rate_debias_at_source(self):
        # The total SoT rate is de-biased once, at the source, so every
        # shot-derived market inherits the calibrated rate consistently.
        books = [_book(), _book()]
        ctx = PriceCtx(home="Argentina", away="Austria", oa=None, oa_event=None,
                       af_books=books)
        raw = _infer_total_rate(books, 87)
        self.assertAlmostEqual(sum(_shot_model(ctx)), raw * SHOT_RATE_DEBIAS, places=9)


class EmpiricalPricingTests(unittest.TestCase):
    def setUp(self):
        self.ctx = PriceCtx(
            home="Argentina", away="Austria", oa=None, oa_event=None,
            af_books=[_book(), _book()],
        )

    def price(self, question, **intent):
        out, source = price_empirical(question, intent, self.ctx)
        self.assertEqual(source, "empirical")
        return out["probability"]

    def test_team_shots_full_match_exceed_second_half(self):
        full = self.price(
            "Will Argentina have 6 or more shots on target?",
            market="team_shots_on_target", subject="home", comparator="gte",
            threshold=6, period="match",
        )
        half = self.price(
            "Will Argentina have 6 or more shots on target in the second half?",
            market="team_shots_on_target", subject="home", comparator="gte",
            threshold=6, period="2H",
        )
        self.assertGreater(full, half)

    def test_stronger_team_is_more_likely_to_win_shots_comparison(self):
        p = self.price(
            "Will Argentina have more shots on target than Austria in the second half?",
            market="team_shots_on_target", subject="home", comparator="more",
            threshold=None, period="2H",
        )
        self.assertGreater(p, 0.5)

    def test_shots_on_target_compare_half_routes_to_more_model(self):
        # The parser emits `shots_on_target_compare` for "more SoT than"; with no
        # bookmaker half market it must reach the team-shots "more" model, not WEB.
        p = self.price(
            "Will Argentina have more shots on target than Austria in the second half?",
            market="shots_on_target_compare", subject="home", comparator="more",
            threshold=None, period="2H",
        )
        self.assertGreater(p, 0.5)  # home is favoured in the fixture
        self.assertLess(p, 1.0)

    def test_both_teams_shot_probability_is_bounded(self):
        p = self.price(
            "Will both teams have at least 1 shot on target in the second half?",
            market="none", subject="match", comparator="yes", threshold=None,
            period="2H",
        )
        self.assertGreater(p, 0.5)
        self.assertLess(p, 1.0)

    def test_second_half_card_probability_is_plausible(self):
        p = self.price(
            "Will Austria receive at least 1 card in the second half?",
            market="team_cards", subject="away", comparator="gte", threshold=1,
            period="2H",
        )
        self.assertGreater(p, 0.2)
        self.assertLess(p, 0.9)

    def test_first_second_half_scorer_excludes_no_goal(self):
        p = self.price(
            "Will Argentina score the first goal of the second half?",
            market="first_team_to_score", subject="home", comparator="yes",
            threshold=None, period="2H",
        )
        self.assertGreater(p, 0.3)
        self.assertLess(p, 0.8)

    def test_penalty_or_red_is_above_penalty_alone(self):
        penalty = self.price(
            "Will a penalty kick be awarded in the match?",
            market="none", subject="match", comparator="yes", threshold=None,
            period="match",
        )
        union = self.price(
            "Will a penalty kick be awarded OR a red card be shown in the match?",
            market="none", subject="match", comparator="yes", threshold=None,
            period="match",
        )
        self.assertGreater(union, penalty)
        self.assertLess(union, 0.5)


def _book():
    return {"bets": [
        {"id": 1, "values": [
            {"value": "Home", "odd": "1.60"}, {"value": "Draw", "odd": "4.00"},
            {"value": "Away", "odd": "6.00"},
        ]},
        {"id": 87, "values": [
            {"value": "Over 7.5", "odd": "1.91"},
            {"value": "Under 7.5", "odd": "1.91"},
        ]},
        {"id": 176, "values": [
            {"value": "Home", "odd": "1.55"}, {"value": "Draw", "odd": "7.00"},
            {"value": "Away", "odd": "4.50"},
        ]},
        {"id": 82, "values": [
            {"value": "Over 1.5", "odd": "1.91"},
            {"value": "Under 1.5", "odd": "1.91"},
        ]},
        {"id": 83, "values": [
            {"value": "Over 1.5", "odd": "2.20"},
            {"value": "Under 1.5", "odd": "1.70"},
        ]},
        {"id": 156, "values": [
            {"value": "Over 1.5", "odd": "1.91"},
            {"value": "Under 1.5", "odd": "1.91"},
        ]},
        {"id": 162, "values": [
            {"value": "Home", "odd": "2.10"}, {"value": "Draw", "odd": "3.00"},
            {"value": "Away", "odd": "3.20"},
        ]},
        {"id": 115, "values": [
            {"value": "Yes", "odd": "1.45"}, {"value": "No", "odd": "2.60"},
        ]},
        {"id": 117, "values": [
            {"value": "Yes", "odd": "2.60"}, {"value": "No", "odd": "1.45"},
        ]},
        {"id": 99, "values": [
            {"value": "Home", "odd": "6.00"}, {"value": "Away", "odd": "12.00"},
        ]},
        {"id": 86, "values": [{"value": "Yes", "odd": "9.00"}]},
    ]}


if __name__ == "__main__":
    unittest.main()
