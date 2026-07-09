import copy
import tempfile
import unittest
from pathlib import Path

from bot.intent_resolution import (
    PARSER_SCHEMA_VERSION,
    RESPONSE_SCHEMA_VERSION,
    IntentResolutionConflictError,
    IntentResolutionError,
    build_resolution_request,
    install_resolution_response,
    validate_resolution_response,
)
from bot.parser import parse_questions


def _intent(**overrides):
    value = {
        "market": "none",
        "subject": "match",
        "player": None,
        "comparator": "yes",
        "threshold": None,
        "period": "match",
        "time_scope": "full_match",
        "excludes_own_goals": False,
    }
    value.update(overrides)
    return value


class IntentResolutionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.registry = Path(self.temp.name) / "registry"
        self.questions = [
            {"id": "known", "question": "Will Argentina win the match?"},
            {"id": "new", "question": "Could something unusual occur?"},
        ]
        parsed = parse_questions(
            self.questions, "Argentina", "Austria", registry_dir=self.registry,
        )
        self.request = build_resolution_request(
            match_id="match-1",
            kickoff="2026-07-10T20:00:00Z",
            home="Argentina",
            away="Austria",
            questions=self.questions,
            unresolved=parsed.unresolved,
        )
        self.response = {
            "schema_version": RESPONSE_SCHEMA_VERSION,
            "request_id": self.request["request_id"],
            "parser_schema_version": PARSER_SCHEMA_VERSION,
            "match_id": "match-1",
            "question_set_hash": self.request["question_set_hash"],
            "resolutions": [{
                "market_id": "new",
                "question": "Could something unusual occur?",
                "intent": _intent(),
            }],
        }

    def tearDown(self):
        self.temp.cleanup()

    def test_install_is_append_only_and_parser_reuses_exact_resolution(self):
        accepted = install_resolution_response(
            self.request, self.response, registry_dir=self.registry,
        )
        self.assertEqual(
            accepted["new"]["intent_source"], "manual-codex-resolution",
        )
        path = Path(accepted["new"]["registry_path"])
        original = path.read_bytes()

        # Installing the identical canonical answer is idempotent and does not
        # rewrite the first provenance record.
        install_resolution_response(
            self.request, self.response, registry_dir=self.registry,
        )
        self.assertEqual(path.read_bytes(), original)

        parsed = parse_questions(
            self.questions, "Argentina", "Austria", registry_dir=self.registry,
        )
        self.assertFalse(parsed.unresolved)
        self.assertEqual(parsed["new"], _intent())
        self.assertEqual(parsed.intent_sources["known"], "tracked-rule")
        self.assertEqual(parsed.intent_sources["new"], "runtime-resolution")
        self.assertEqual(
            parsed.resolution_provenance["new"]["registry_path"], str(path),
        )

    def test_conflicting_answer_never_overwrites_registry(self):
        accepted = install_resolution_response(
            self.request, self.response, registry_dir=self.registry,
        )
        path = Path(accepted["new"]["registry_path"])
        original = path.read_bytes()
        conflict = copy.deepcopy(self.response)
        conflict["resolutions"][0]["intent"] = _intent(
            market="red_card", time_scope="regulation",
        )
        with self.assertRaises(IntentResolutionConflictError):
            install_resolution_response(
                self.request, conflict, registry_dir=self.registry,
            )
        self.assertEqual(path.read_bytes(), original)

    def test_response_must_match_request_and_question_snapshot(self):
        mutations = []
        wrong_request = copy.deepcopy(self.response)
        wrong_request["request_id"] = "0" * 64
        mutations.append(wrong_request)
        wrong_match = copy.deepcopy(self.response)
        wrong_match["match_id"] = "match-2"
        mutations.append(wrong_match)
        wrong_questions = copy.deepcopy(self.response)
        wrong_questions["question_set_hash"] = "0" * 64
        mutations.append(wrong_questions)
        wrong_wording = copy.deepcopy(self.response)
        wrong_wording["resolutions"][0]["question"] += "!"
        mutations.append(wrong_wording)
        stale = copy.deepcopy(self.response)
        stale["parser_schema_version"] = "old-parser"
        mutations.append(stale)

        for response in mutations:
            with self.subTest(response=response):
                with self.assertRaises(IntentResolutionError):
                    validate_resolution_response(self.request, response)

    def test_response_rejects_missing_extra_and_malformed_items(self):
        missing = copy.deepcopy(self.response)
        missing["resolutions"] = []
        extra = copy.deepcopy(self.response)
        extra["resolutions"].append({
            "market_id": "known",
            "question": "Will Argentina win the match?",
            "intent": _intent(),
        })
        malformed = copy.deepcopy(self.response)
        del malformed["resolutions"][0]["intent"]["period"]
        unknown_field = copy.deepcopy(self.response)
        unknown_field["resolutions"][0]["intent"]["explanation"] = "guess"

        for response in (missing, extra, malformed, unknown_field):
            with self.subTest(response=response):
                with self.assertRaises(IntentResolutionError):
                    validate_resolution_response(self.request, response)

    def test_compound_decomposition_is_strict_and_round_trips(self):
        self.response["resolutions"][0]["compound"] = {
            "op": "OR",
            "components": [
                {
                    "question": "Will a penalty kick be awarded?",
                    "intent": _intent(
                        market="penalty_awarded", time_scope="regulation",
                    ),
                },
                {
                    "question": "Will a red card be shown?",
                    "intent": _intent(market="red_card", time_scope="regulation"),
                },
            ],
        }
        install_resolution_response(
            self.request, self.response, registry_dir=self.registry,
        )
        parsed = parse_questions(
            self.questions, "Argentina", "Austria", registry_dir=self.registry,
        )
        self.assertEqual(parsed.compounds["new"]["op"], "OR")
        self.assertEqual(len(parsed.compounds["new"]["components"]), 2)

    def test_resolution_is_bound_to_exact_question_and_teams(self):
        install_resolution_response(
            self.request, self.response, registry_dir=self.registry,
        )
        changed_question = parse_questions(
            [{"id": "new", "question": "Could something unusual happen?"}],
            "Argentina", "Austria", registry_dir=self.registry,
        )
        changed_teams = parse_questions(
            [{"id": "new", "question": "Could something unusual occur?"}],
            "Argentina", "Brazil", registry_dir=self.registry,
        )
        self.assertEqual(len(changed_question.unresolved), 1)
        self.assertEqual(len(changed_teams.unresolved), 1)

    def test_question_set_hash_is_independent_of_provider_order(self):
        from bot.intent_resolution import question_set_hash

        self.assertEqual(
            question_set_hash(self.questions),
            question_set_hash(list(reversed(self.questions))),
        )


if __name__ == "__main__":
    unittest.main()
