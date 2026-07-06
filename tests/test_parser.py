import unittest
import json
from unittest.mock import patch

from bot.parser import _normalize_question, _repair_intent, parse_questions


class DeterministicTemplateTests(unittest.TestCase):
    def test_recurring_templates_do_not_call_the_llm(self):
        cases = [
            ("Will Austria be caught offside 2 or more times?",
             "team_offsides", "away", "gte", 2, "match"),
            ("Will the second half have more goals than the first half?",
             "highest_scoring_half_2h", "match", "second_half_more", None, "match"),
            ("Will Austria finish with more corner kicks than Argentina?",
             "corners_compare", "away", "more", None, "match"),
            ("Will Argentina win the match?",
             "match_winner", "home", "win", None, "match"),
            ("Will the match have 2 or fewer total goals?",
             "total_goals", "match", "lte", 2, "match"),
            ("Will there be 4 or more total cards shown?",
             "total_cards", "match", "gte", 4, "match"),
            ("Will Argentina score in the second half?",
             "team_score_2h", "home", "yes", None, "2H"),
            ("Will Marcel Sabitzer have at least 1 shot on target?",
             "player_shots_on_target", "player", "gte", 1, "match"),
            ("Will Austria have more shots on target than Argentina in the second half?",
             "shots_on_target_compare", "away", "more", None, "2H"),
            ("At halftime, will Austria have more corner kicks than Argentina?",
             "corners_compare", "away", "more", None, "1H"),
            ("Will Sadio Mané score a goal (excluding own goals)?",
             "player_goal_scorer", "player", "yes", None, "match"),
            ("Will there be 9 or more total corner kicks?",
             "total_corners", "match", "gte", 9, "match"),
        ]
        questions = [
            {"id": str(i), "question": case[0]} for i, case in enumerate(cases)
        ]
        with patch("bot.parser.chat_json") as chat:
            parsed = parse_questions(questions, "Argentina", "Austria")
        chat.assert_not_called()

        for i, (_, market, subject, comparator, threshold, period) in enumerate(cases):
            intent = parsed[str(i)]
            self.assertEqual(
                (intent["market"], intent["subject"], intent["comparator"],
                 intent["threshold"], intent["period"]),
                (market, subject, comparator, threshold, period),
            )

    def test_only_unfamiliar_questions_are_sent_to_the_llm(self):
        questions = [
            {"id": "known", "question": "Will Argentina win the match?"},
            {"id": "new", "question": "Could something unusual occur?"},
        ]
        response = json.dumps({"intents": [{
            "id": 0, "market": "none", "subject": "match",
            "player": None, "comparator": "yes", "threshold": None,
            "period": "match",
        }]})
        with patch("bot.parser.config.OPENAI_API_KEY", "key"), patch(
            "bot.parser.chat_json", return_value=response
        ) as chat:
            parsed = parse_questions(questions, "Argentina", "Austria")

        sent = chat.call_args.args[0][1]["content"]
        self.assertIn("Could something unusual occur?", sent)
        self.assertNotIn("Will Argentina win the match?", sent)
        self.assertEqual(parsed["known"]["market"], "match_winner")
        self.assertEqual(parsed["new"]["market"], "none")

    def test_known_templates_work_without_an_api_key(self):
        with patch("bot.parser.config.OPENAI_API_KEY", ""), patch(
            "bot.parser.chat_json"
        ) as chat:
            parsed = parse_questions(
                [{"id": "x", "question": "Will Argentina win the match?"}],
                "Argentina", "Austria",
            )
        chat.assert_not_called()
        self.assertEqual(parsed["x"]["subject"], "home")

    def test_team_code_in_question_is_not_parsed_as_a_player(self):
        with patch("bot.parser.chat_json") as chat:
            parsed = parse_questions(
                [{"id": "x", "question":
                  "Will USA have 6 or more shots on target?"}],
                "USA", "Australia",
            )
        chat.assert_not_called()
        self.assertEqual(parsed["x"]["market"], "team_shots_on_target")
        self.assertEqual(parsed["x"]["subject"], "home")


class SubjectRepairTests(unittest.TestCase):
    def test_named_team_total_is_repaired(self):
        intent = {"market": "total_offsides", "subject": "match"}
        repaired = _repair_intent(
            "Will Austria be caught offside 2 or more times?",
            intent, "Argentina", "Austria",
        )
        self.assertEqual(repaired["subject"], "away")
        self.assertEqual(intent["subject"], "match")

    def test_provider_word_order_and_punctuation_are_tolerated(self):
        intent = {"market": "total_fouls", "subject": "match"}
        repaired = _repair_intent(
            "Will DR Congo commit 12 or more fouls?",
            intent, "Colombia", "Congo DR",
        )
        self.assertEqual(repaired["subject"], "away")

    def test_match_total_stays_match_scoped(self):
        intent = {"market": "total_cards", "subject": "match"}
        repaired = _repair_intent(
            "Will there be 4 or more total cards shown?",
            intent, "Argentina", "Austria",
        )
        self.assertEqual(repaired["subject"], "match")

    def test_literal_team_subject_is_normalized(self):
        repaired = _repair_intent(
            "Will Norway win the match?",
            {"market": "match_winner", "subject": "Norway"},
            "Norway", "France",
        )
        self.assertEqual(repaired["subject"], "home")

    def test_numeric_second_half_goals_are_not_highest_half(self):
        repaired = _repair_intent(
            "Will the second half have 2 or more total goals?",
            {"market": "highest_scoring_half_2h", "subject": "match", "comparator": "gte"},
            "Portugal", "Uzbekistan",
        )
        self.assertEqual(repaired["market"], "total_goals")

    def test_score_or_assist_is_not_reduced_to_scorer(self):
        repaired = _repair_intent(
            "Will Orkun Kökçü score or assist a goal?",
            {"market": "player_goal_scorer", "subject": "player"},
            "Türkiye", "USA",
        )
        self.assertEqual(repaired["market"], "player_score_or_assist")

    def test_numeric_offside_is_not_reduced_to_comparison(self):
        repaired = _repair_intent(
            "Will Türkiye be caught offside 2 or more times?",
            {"market": "offsides_compare", "subject": "home", "comparator": "gte"},
            "Türkiye", "USA",
        )
        self.assertEqual(repaired["market"], "team_offsides")

    def test_at_halftime_tied_maps_to_first_half_draw(self):
        repaired = _repair_intent(
            "At halftime, will the match be tied?",
            {"market": "none", "subject": "match", "comparator": "yes",
             "threshold": None, "period": "match"},
            "Norway", "Senegal",
        )
        self.assertEqual(repaired["market"], "match_draw")
        self.assertEqual(repaired["subject"], "match")
        self.assertEqual(repaired["period"], "1H")

    def test_second_half_total_goals_sets_period(self):
        repaired = _repair_intent(
            "Will the second half have 2 or more total goals?",
            {"market": "total_goals", "subject": "match", "comparator": "gte",
             "threshold": 2, "period": "match"},
            "Portugal", "Uzbekistan",
        )
        self.assertEqual(repaired["market"], "total_goals")
        self.assertEqual(repaired["period"], "2H")

    def test_outscore_opponent_in_half_is_match_winner(self):
        repaired = _repair_intent(
            "Will Senegal score more goals than Norway in the second half?",
            {"market": "total_goals", "subject": "match", "comparator": "more",
             "threshold": None, "period": "match"},
            "Norway", "Senegal",
        )
        self.assertEqual(repaired["market"], "match_winner")
        self.assertEqual(repaired["comparator"], "win")
        self.assertEqual(repaired["subject"], "away")  # Senegal is the away team
        self.assertEqual(repaired["period"], "2H")

    def test_highest_scoring_half_keeps_match_period(self):
        repaired = _repair_intent(
            "Will the second half have more goals than the first half?",
            {"market": "highest_scoring_half_2h", "subject": "match",
             "comparator": "second_half_more", "threshold": None, "period": "match"},
            "Portugal", "Uzbekistan",
        )
        self.assertEqual(repaired["market"], "highest_scoring_half_2h")
        self.assertEqual(repaired["period"], "match")

    def test_offside_phrase_repairs_all_required_fields(self):
        repaired = _repair_intent(
            "Will Austria be caught offside 2 or more times?",
            {"market": "none", "subject": "match", "comparator": "yes",
             "threshold": 2, "period": "2H"},
            "Argentina", "Austria",
        )
        self.assertEqual(repaired["market"], "team_offsides")
        self.assertEqual(repaired["subject"], "away")
        self.assertEqual(repaired["comparator"], "gte")
        self.assertEqual(repaired["period"], "match")


class KnockoutWordingTests(unittest.TestCase):
    """Best-of-32 questions: regulation suffix, (Country) props, new families."""

    def _parse(self, question, home, away):
        with patch("bot.parser.chat_json") as chat:
            parsed = parse_questions(
                [{"id": "x", "question": question}], home, away
            )
        chat.assert_not_called()  # must stay deterministic (no LLM spend)
        return parsed["x"]

    def test_regulation_suffix_is_stripped(self):
        self.assertEqual(
            _normalize_question(
                "Will Germany win in regulation (90 minutes + stoppage time)?",
                "Germany", "Paraguay"),
            "Will Germany win?",
        )
        self.assertEqual(
            _normalize_question(
                "Will the second half produce more goals than the first half in "
                "regulation (90 minutes + stoppage time), excluding extra time?",
                "Australia", "Egypt"),
            "Will the second half produce more goals than the first half?",
        )

    def test_regulation_and_full_match_scopes_remain_distinct(self):
        regulation = self._parse(
            "Will a goal be scored after the second hydration break in regulation "
            "(90 minutes + stoppage time)?",
            "Ivory Coast", "Norway",
        )
        full_match = self._parse(
            "Will a goal be scored after the second hydration break?",
            "Ivory Coast", "Norway",
        )
        self.assertEqual(regulation["time_scope"], "regulation")
        self.assertEqual(full_match["time_scope"], "full_match")

    def test_unqualified_first_goal_and_red_card_include_extra_time(self):
        first = self._parse(
            "Will France score the first goal of the match?", "France", "Sweden",
        )
        red = self._parse(
            "Will a red card be shown in the match?", "France", "Sweden",
        )
        self.assertEqual(first["time_scope"], "full_match")
        self.assertEqual(red["time_scope"], "full_match")

    def test_team_card_90_minute_scope_is_regulation(self):
        intent = self._parse(
            "Will France receive at least 1 card (90 minutes + stoppage time)?",
            "France", "Sweden",
        )
        self.assertEqual(
            (intent["market"], intent["subject"], intent["comparator"],
             intent["threshold"], intent["period"], intent["time_scope"]),
            ("team_cards", "home", "gte", 1, "match", "regulation"),
        )

    def test_goal_method_and_team_score_templates_are_deterministic(self):
        own = self._parse(
            "Will an own goal be scored in regulation (90 minutes + stoppage time)?",
            "Mexico", "Ecuador",
        )
        outside = self._parse(
            "Will a goal be scored from outside the penalty area in regulation "
            "(90 minutes + stoppage time)?",
            "Mexico", "Ecuador",
        )
        team = self._parse(
            "Will DR Congo score a goal (excluding own goals) in regulation "
            "(90 minutes + stoppage time)?",
            "England", "Congo DR",
        )
        self.assertEqual(own["market"], "own_goal")
        self.assertEqual(outside["market"], "none")
        self.assertEqual((team["market"], team["subject"]), ("team_score", "away"))

    def test_country_parenthetical_is_removed(self):
        self.assertEqual(
            _normalize_question(
                "Will Jamal Musiala (Germany) have 2 or more shots on target "
                "in regulation (90 minutes + stoppage time)?",
                "Germany", "Paraguay"),
            "Will Jamal Musiala have 2 or more shots on target?",
        )

    def test_player_prop_with_country_is_player_not_team(self):
        # The (Germany) parenthetical previously made this Germany's team SoT.
        intent = self._parse(
            "Will Jamal Musiala (Germany) have 2 or more shots on target "
            "in regulation (90 minutes + stoppage time)?",
            "Germany", "Paraguay",
        )
        self.assertEqual(intent["market"], "player_shots_on_target")
        self.assertEqual(intent["subject"], "player")
        self.assertEqual(intent["player"], "Jamal Musiala")
        self.assertEqual((intent["comparator"], intent["threshold"]), ("gte", 2))

    def test_player_scorer_and_assist_with_country(self):
        scorer = self._parse(
            "Will Kai Havertz (Germany) score a goal (excluding own goals) "
            "in regulation (90 minutes + stoppage time)?",
            "Germany", "Paraguay",
        )
        self.assertEqual(scorer["market"], "player_goal_scorer")
        self.assertEqual(scorer["player"], "Kai Havertz")
        assist = self._parse(
            "Will Florian Wirtz (Germany) score or assist a goal (excluding own "
            "goals) in regulation (90 minutes + stoppage time)?",
            "Germany", "Paraguay",
        )
        self.assertEqual(assist["market"], "player_score_or_assist")
        self.assertEqual(assist["player"], "Florian Wirtz")

    def test_win_by_margin_is_not_a_scoring_total(self):
        intent = self._parse(
            "Will the United States win by 2 or more goals in regulation "
            "(90 minutes + stoppage time)?",
            "USA", "Bosnia and Herzegovina",
        )
        self.assertEqual(intent["market"], "win_margin")
        self.assertEqual(intent["subject"], "home")
        self.assertEqual((intent["comparator"], intent["threshold"]), ("gte", 2))

    def test_score_two_or_more_goals_stays_team_total(self):
        intent = self._parse(
            "Will Brazil score 2 or more goals in regulation "
            "(90 minutes + stoppage time)?",
            "Brazil", "Japan",
        )
        self.assertEqual(intent["market"], "team_total_goals")
        self.assertEqual((intent["subject"], intent["comparator"],
                          intent["threshold"]), ("home", "gte", 2))

    def test_new_knockout_families(self):
        cases = [
            ("Will South Africa advance to the Round of 16?",
             "South Africa", "Canada", "to_advance", "home", "match"),
            ("Will Argentina keep a clean sheet in regulation "
             "(90 minutes + stoppage time)?",
             "Argentina", "Cape Verde", "team_clean_sheet", "home", "match"),
            ("Will Argentina score in both halves in regulation "
             "(90 minutes + stoppage time)?",
             "Argentina", "Cape Verde", "team_score_both_halves", "home", "match"),
            ("Will Germany win in regulation (90 minutes + stoppage time)?",
             "Germany", "Paraguay", "match_winner", "home", "match"),
            ("Will Canada be ahead at halftime?",
             "South Africa", "Canada", "match_winner", "away", "1H"),
            ("Will regulation (90 minutes + stoppage time) end in a tie?",
             "South Africa", "Canada", "match_draw", "match", "match"),
            ("Will the match be tied at halftime?",
             "Australia", "Egypt", "match_draw", "match", "1H"),
            ("Will Germany score the first goal of the match?",
             "Germany", "Paraguay", "first_team_to_score", "home", "match"),
            ("Will the second half produce more goals than the first half in "
             "regulation (90 minutes + stoppage time)?",
             "Australia", "Egypt", "highest_scoring_half_2h", "match", "match"),
            ("Will there be 22 or more total shots (on and off target) in "
             "regulation (90 minutes + stoppage time)?",
             "Argentina", "Cape Verde", "total_shots", "match", "match"),
            ("Will both teams receive at least one card in regulation "
             "(90 minutes + stoppage time)?",
             "Australia", "Egypt", "both_teams_card", "match", "match"),
            ("Will a card be shown in the first half?",
             "USA", "Bosnia and Herzegovina", "total_cards", "match", "1H"),
            ("Will a penalty kick be awarded during regulation "
             "(90 minutes + stoppage time)?",
             "Ivory Coast", "Norway", "penalty_awarded", "match", "match"),
            ("Will a red card be shown in the match?",
             "France", "Sweden", "red_card", "match", "match"),
        ]
        for q, home, away, market, subject, period in cases:
            intent = self._parse(q, home, away)
            self.assertEqual(
                (intent["market"], intent["subject"], intent["period"]),
                (market, subject, period), msg=q,
            )

    def test_no_market_families_route_to_none_without_a_half_period(self):
        # Hydration breaks are at 22'/70' boundaries — never a 45' half boundary.
        for q in (
            "Will a card be shown after the second hydration break, including "
            "any extra time?",
            "Will either team be ruled offside before the first hydration break?",
            "Will a goal be scored in second-half stoppage time?",
            "Will any player score more than 1 goal (excluding own goals) in "
            "regulation (90 minutes + stoppage time)?",
        ):
            intent = self._parse(q, "South Africa", "Canada")
            self.assertEqual(intent["market"], "none", msg=q)
            self.assertEqual(intent["period"], "match", msg=q)

    def test_current_round_of_16_specials_are_deterministic(self):
        cases = [
            (
                "Will Morocco advance to the quarterfinals?",
                "Canada", "Morocco", "to_advance", "away",
            ),
            (
                "Will Brazil advance to the quarterfinals?",
                "Brazil", "Norway", "to_advance", "home",
            ),
            (
                "Will the match be decided by a penalty shootout?",
                "Canada", "Morocco", "penalty_shootout", "match",
            ),
            (
                "Will exactly 1 goal be scored in regulation "
                "(90 minutes + stoppage time)?",
                "Canada", "Morocco", "total_goals", "match",
            ),
            (
                "Will the match finish with exactly 2 total goals in regulation "
                "(90 minutes + stoppage time)?",
                "Brazil", "Norway", "total_goals", "match",
            ),
            (
                "Will Paraguay hold a lead at any point in the match "
                "(excluding a penalty shootout)?",
                "Paraguay", "France", "lead_any_time", "home",
            ),
            (
                "Will there be more total cards than total goals in regulation "
                "(90 minutes + stoppage time)?",
                "Mexico", "England", "cards_more_than_goals", "match",
            ),
            (
                "Will Christian Pulisic (United States) play the entire match "
                "in regulation (90 minutes + stoppage time)?",
                "USA", "Belgium", "player_full_match", "player",
            ),
            (
                "Will at least one goal be scored in each half in regulation "
                "(90 minutes + stoppage time)?",
                "USA", "Belgium", "goal_in_each_half", "match",
            ),
        ]
        for q, home, away, market, subject in cases:
            intent = self._parse(q, home, away)
            self.assertEqual((intent["market"], intent["subject"]),
                             (market, subject), msg=q)
            if "decided by a penalty shootout" in q:
                self.assertEqual(intent["time_scope"], "penalty_shootout")
            if "exactly 1 goal" in q:
                self.assertEqual((intent["comparator"], intent["threshold"]), ("eq", 1))
            if "exactly 2 total goals" in q:
                self.assertEqual((intent["comparator"], intent["threshold"]), ("eq", 2))
            if "Christian Pulisic" in q:
                self.assertEqual(intent["player"], "Christian Pulisic")

    def test_portugal_spain_open_questions_are_deterministic(self):
        cases = [
            (
                "Will Cristiano Ronaldo (Portugal) score a goal (excluding own goals) "
                "in regulation (90 minutes + stoppage time)?",
                "player_goal_scorer", "player", "Cristiano Ronaldo", "regulation",
            ),
            (
                "Will Lamine Yamal (Spain) score or assist a goal (excluding own goals) "
                "in regulation (90 minutes + stoppage time)?",
                "player_score_or_assist", "player", "Lamine Yamal", "regulation",
            ),
            (
                "Will Bruno Fernandes (Portugal) have 1 or more shots on target "
                "in regulation (90 minutes + stoppage time)?",
                "player_shots_on_target", "player", "Bruno Fernandes", "regulation",
            ),
            (
                "Will both halves have the same number of goals in regulation "
                "(90 minutes + stoppage time)?",
                "highest_scoring_half_draw", "match", None, "regulation",
            ),
            (
                "Will Portugal score the first goal of the match in regulation "
                "(90 minutes + stoppage time)?",
                "first_team_to_score", "home", None, "regulation",
            ),
            (
                "Will the match have 3 or more total goals in regulation "
                "(90 minutes + stoppage time)?",
                "total_goals", "match", None, "regulation",
            ),
            (
                "Will Diogo Costa (Portugal) make 4 or more saves in regulation "
                "(90 minutes + stoppage time)?",
                "player_goalkeeper_saves", "player", "Diogo Costa", "regulation",
            ),
            (
                "Will a substitute score a goal (excluding own goals) in regulation "
                "(90 minutes + stoppage time)?",
                "substitute_score", "match", None, "regulation",
            ),
            (
                "Will there be 4 or more total cards shown in regulation "
                "(90 minutes + stoppage time)?",
                "total_cards", "match", None, "regulation",
            ),
            (
                "Will there be 9 or more total substitutions (both teams combined) "
                "in regulation (90 minutes + stoppage time)?",
                "total_substitutions", "match", None, "regulation",
            ),
            (
                "Will Spain have 6 or more corner kicks in regulation "
                "(90 minutes + stoppage time)?",
                "team_corners", "away", None, "regulation",
            ),
            (
                "Will the match go to extra time?",
                "goes_to_extra_time", "match", None, "full_match",
            ),
            (
                "Will any Portugal player have 2 or more shots on target in regulation "
                "(90 minutes + stoppage time)?",
                "any_team_player_shots_on_target", "home", None, "regulation",
            ),
            (
                "Will Spain advance to the quarterfinals?",
                "to_advance", "away", None, "full_match",
            ),
            (
                "Will the first card of the match be shown before the first goal is scored?",
                "first_card_before_first_goal", "match", None, "full_match",
            ),
        ]
        questions = [{"id": str(i), "question": q} for i, (q, *_rest) in enumerate(cases)]
        with patch("bot.parser.chat_json") as chat:
            parsed = parse_questions(questions, "Portugal", "Spain")
        chat.assert_not_called()

        for i, (q, market, subject, player, scope) in enumerate(cases):
            intent = parsed[str(i)]
            self.assertEqual((intent["market"], intent["subject"], intent["time_scope"]),
                             (market, subject, scope), msg=q)
            if player:
                self.assertEqual(intent["player"], player, msg=q)
            if "2 or more shots on target" in q:
                self.assertEqual((intent["comparator"], intent["threshold"]), ("gte", 2))
            if "4 or more saves" in q:
                self.assertEqual((intent["comparator"], intent["threshold"]), ("gte", 4))
            if "9 or more total substitutions" in q:
                self.assertEqual((intent["comparator"], intent["threshold"]), ("gte", 9))

    def test_special_familiar_model_families(self):
        hydration = self._parse(
            "Will a goal be scored before the first hydration break?",
            "South Africa", "Canada",
        )
        substitute = self._parse(
            "Will a substitute score a goal in regulation "
            "(90 minutes + stoppage time)?",
            "South Africa", "Canada",
        )
        self.assertEqual((hydration["market"], hydration["period"]),
                         ("goal_window", "match"))
        self.assertEqual((substitute["market"], substitute["period"]),
                         ("substitute_score", "match"))


if __name__ == "__main__":
    unittest.main()
