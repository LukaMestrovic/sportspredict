"""Tests for the bundled simulator bridge."""
import json
import subprocess
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from bot import simulator
from bot.odds_context import PriceCtx


def _ctx():
    return PriceCtx("Brazil", "Japan", af_books=[], oa=None, oa_event=None)


def _bridge_response(reports, *, unsupported=None, model=None):
    return {
        "schema_version": "2.1",
        "match": {"team_a": "Brazil", "team_b": "Japan", "stage": "group"},
        "model": model or {
            "engine": "SimulatorEngine", "rate_model": "LearnedRateModel",
            "n_sims": 8000,
        },
        "evidence_instruction": "Model context only: weigh against odds…",
        "question_reports": reports,
        "unsupported_questions": unsupported or [],
    }


def _unavailable_evidence(key):
    """A schema-2.1 historical_evidence block with every scope unavailable."""
    missing = {"available": False, "reason": "No exact historical label set for this contract."}
    return {
        "contract_key": key,
        "model_performance": {"all_history": dict(missing), "wc2026": dict(missing)},
        "empirical_rate": {"all_history": dict(missing), "wc2026": dict(missing)},
    }


def _populated_evidence(key):
    """A historical_evidence block mixing populated and explicitly-unavailable scopes."""
    return {
        "contract_key": key,
        "empirical_rate": {
            "all_history": {"available": True, "matches": 2974, "observations": 2974,
                            "rate": 0.234701, "yes_events": 698},
            "wc2026": {"available": True, "matches": 34, "observations": 34,
                       "rate": 0.382353, "yes_events": 13},
        },
        "model_performance": {
            "all_history": {"available": True, "always_50_brier": 0.25, "brier": 0.168899,
                            "delta_vs_always_50": -0.081101, "matches": 2277, "questions": 2277,
                            "test_folds": [2021, 2022, 2023, 2024, 2025, 2026]},
            "wc2026": {"available": False, "reason": "No unseen settled questions for this contract."},
        },
    }


def _report(market_id, question, family, probability, *, contract_key=None,
            historical_evidence=None):
    key = contract_key or f"{family}:reg"
    return {
        "market_id": market_id,
        "question": question,
        "source": "sportspredict-simulator",
        "family": family,
        "contract_key": key,
        "probability": probability,
        "probability_pct": round(probability * 100, 2),
        "explanation": f"Deterministic basis for {family}.",
        "historical_evidence": (
            historical_evidence if historical_evidence is not None else _unavailable_evidence(key)
        ),
        "evidence_role": "model_context",
    }


class ReportParsingTests(unittest.TestCase):
    """Mapping of a successful report into per-market estimate items."""

    def _run(self, markets, direct_by_market, bridge_response, **kwargs):
        run = MagicMock(return_value=bridge_response)
        with patch.object(simulator, "_runtime",
                          return_value=(Path("/fake/root"), Path("/fake/py"))), \
                patch.object(simulator, "_run_bridge", run):
            out = simulator.simulator_estimates(
                markets, _ctx(), direct_by_market=direct_by_market, **kwargs)
        return out, run

    def test_successful_report_is_keyed_by_market_id(self):
        markets = [{"id": "m1", "question": "Will a goal be scored before the first hydration break?"}]
        resp = _bridge_response([_report("m1", markets[0]["question"], "goal_window", 0.402)])
        out, _ = self._run(markets, {"m1": []}, resp)

        self.assertIn("m1", out)
        item = out["m1"]
        self.assertEqual(item["source"], "sportspredict-simulator")
        self.assertEqual(item["family"], "goal_window")
        self.assertEqual(item["probability"], 0.402)
        self.assertEqual(item["probability_pct"], 40.2)
        self.assertEqual(item["evidence_role"], "model_context")
        self.assertIn("Deterministic basis", item["explanation"])
        self.assertEqual(item["contract_key"], "goal_window:reg")
        self.assertIn("model context only", item["adjustment_guidance"])
        self.assertEqual(item["model"]["rate_model"], "LearnedRateModel")
        self.assertEqual(item["model"]["n_sims"], 8000)
        self.assertNotIn("odds_anchor_applied", item["model"])
        self.assertIn("not a final anchor", item["note"])

    def test_schema2_evidence_fields_pass_through_unchanged(self):
        markets = [{"id": "brace", "question": "Will any player score a brace?"}]
        key = "any_player_threshold:goals:>:1:reg"
        hist = _populated_evidence(key)
        resp = _bridge_response([_report(
            "brace", markets[0]["question"], "any_player_threshold", 0.2347,
            contract_key=key,
            historical_evidence=hist)])
        out, _ = self._run(markets, {"brace": []}, resp)

        item = out["brace"]
        self.assertEqual(item["contract_key"], key)
        self.assertIn("expected minutes", item["adjustment_guidance"])
        # historical_evidence is carried through verbatim, populated AND unavailable scopes.
        self.assertEqual(item["historical_evidence"], hist)
        self.assertEqual(
            item["historical_evidence"]["empirical_rate"]["all_history"]["rate"], 0.234701)
        self.assertIs(
            item["historical_evidence"]["model_performance"]["wc2026"]["available"], False)

    def test_explicitly_unavailable_scopes_survive(self):
        markets = [{"id": "pen", "question": "Will a penalty kick be awarded in the match?"}]
        hist = _unavailable_evidence("penalty_awarded:match")
        resp = _bridge_response([_report(
            "pen", markets[0]["question"], "penalty_awarded", 0.235,
            contract_key="penalty_awarded:match", historical_evidence=hist)])
        out, _ = self._run(markets, {"pen": []}, resp)

        self.assertEqual(out["pen"]["historical_evidence"], hist)
        for layer in ("model_performance", "empirical_rate"):
            for scope in ("all_history", "wc2026"):
                self.assertIs(
                    out["pen"]["historical_evidence"][layer][scope]["available"], False)

    def test_missing_schema2_fields_degrade_to_none(self):
        # An older bridge that omits the schema-2.1 fields must not crash; the
        # projection simply yields None for them (fail-soft, no KeyError).
        markets = [{"id": "m1", "question": "Will a goal be scored?"}]
        bare = {
            "market_id": "m1", "question": markets[0]["question"],
            "source": "sportspredict-simulator", "family": "goal_window",
            "probability": 0.4, "probability_pct": 40.0,
            "explanation": "x", "evidence_role": "model_context",
        }
        out, _ = self._run(markets, {"m1": []}, _bridge_response([bare]))
        self.assertIsNone(out["m1"]["contract_key"])
        self.assertIn("Use this as model context only", out["m1"]["adjustment_guidance"])
        self.assertIsNone(out["m1"]["historical_evidence"])

    def test_unsupported_questions_are_omitted(self):
        markets = [
            {"id": "ok", "question": "Will a penalty kick be awarded?"},
            {"id": "bad", "question": "Will the referee teleport?"},
        ]
        resp = _bridge_response(
            [_report("ok", markets[0]["question"], "penalty_awarded", 0.23)],
            unsupported=[{"market_id": "bad", "question": markets[1]["question"],
                          "reason": "No simulator resolver for this exact question template."}],
        )
        out, _ = self._run(markets, {"ok": [], "bad": []}, resp)

        self.assertIn("ok", out)
        self.assertNotIn("bad", out)

    def test_report_with_out_of_range_probability_is_dropped(self):
        markets = [{"id": "m1", "question": "Will a goal be scored?"}]
        resp = _bridge_response([_report("m1", markets[0]["question"], "goal_window", 1.7)])
        out, _ = self._run(markets, {"m1": []}, resp)
        self.assertEqual(out, {})

    def test_new_additive_families_all_reach_estimates(self):
        families = [
            ("hyd_before", "Will a goal be scored before the first hydration break?", "goal_window"),
            ("hyd_after", "Will a goal be scored after the second hydration break?", "goal_window"),
            ("stoppage", "Will a goal be scored in first-half stoppage time?", "goal_window"),
            ("first_scorer", "Will Vinicius Junior score the first goal?", "first_goal"),
            ("compound", "Will both teams score AND over 2.5 goals?", "compound_and"),
            ("card_window", "Will a card be shown after the second hydration break?", "card_window"),
            ("offside_window", "Will either team be offside before the first hydration break?", "stat_window"),
            ("corner_window", "Will there be a corner before the first hydration break?", "stat_window"),
            ("sub_before_half", "Will there be a substitution before halftime?", "substitution_before_half"),
            ("sub_scorer", "Will a substitute score in the match?", "substitute_score"),
            ("sub_involvement", "Will a substitute score or assist a goal in regulation?", "substitute_score_or_assist"),
            ("any_sot", "Will any player have 3 or more shots on target?", "any_player_threshold"),
            ("any_brace", "Will any player score a brace?", "any_player_threshold"),
            ("total_shots", "Will there be over 20 total shots in the match?", "total_shots_threshold"),
            ("win_margin", "Will the home team win by 2 or more goals?", "win_margin"),
            ("first_goal_2h", "Will the first goal of the match be scored in the second half?", "first_goal_half"),
            ("win_both_halves", "Will either team win both halves in regulation?", "win_both_halves"),
            ("exact_margin", "Will the match be decided by exactly 1 goal in regulation?", "exact_goal_margin"),
            ("corners_shots", "Will Brazil have more corner kicks AND more total shots than Japan in regulation?", "team_corners_and_total_shots_more"),
            ("red_card", "Will a red card be shown in the match?", "red_card"),
            ("both_card", "Will both teams receive at least one card?", "both_teams_card"),
            ("card_each_half", "Will at least one card be shown in each half in regulation?", "card_window"),
            ("card_stoppage", "Will a card be shown during first- or second-half stoppage time?", "card_window"),
            ("lead_any", "Will Brazil hold a lead at any point in the match?", "lead_any_time"),
            ("cards_gt_goals", "Will there be more total cards than total goals in regulation?", "cards_more_than_goals"),
            ("full_match", "Will Vinicius Junior play the entire match in regulation?", "player_full_match"),
            ("goal_each_half", "Will at least one goal be scored in each half in regulation?", "half_conditional"),
            ("player_score", "Will Vinicius Junior score in regulation?", "player_score"),
            ("player_soa", "Will Vinicius Junior score or assist in regulation?", "player_score_or_assist"),
        ]
        markets = [{"id": mid, "question": q} for mid, q, _ in families]
        reports = [_report(mid, q, fam, 0.3) for mid, q, fam in families]
        out, _ = self._run(markets, {mid: [] for mid, _, _ in families},
                           _bridge_response(reports))

        for mid, _q, fam in families:
            self.assertIn(mid, out, f"{mid} missing from estimates")
            self.assertEqual(out[mid]["family"], fam)

    def test_team_qualified_any_player_sot_uses_broad_proxy_question(self):
        question = (
            "Will any Portugal player have 2 or more shots on target in regulation "
            "(90 minutes + stoppage time)?"
        )
        markets = [{"id": "any_sot", "question": question}]
        intents = {
            "any_sot": {
                "market": "any_team_player_shots_on_target",
                "subject": "home",
                "comparator": "gte",
                "threshold": 2,
                "period": "match",
            }
        }
        resp = _bridge_response([_report(
            "any_sot",
            "Will any player have 2 or more shots on target in regulation?",
            "any_player_threshold",
            0.83,
            contract_key="any_player_threshold:shots_on_target:>=:2:reg",
        )])
        out, run = self._run(markets, {"any_sot": []}, resp, intents=intents)

        payload = run.call_args.args[0]
        self.assertEqual(
            payload["questions"][0]["question"],
            "Will any player have 2 or more shots on target in regulation?",
        )
        self.assertEqual(
            out["any_sot"]["contract_key"],
            "any_player_threshold:shots_on_target:>=:2:reg",
        )
        self.assertIn("Simulator proxy", out["any_sot"]["proxy_note"])
        self.assertIn("narrower team-specific", out["any_sot"]["adjustment_guidance"])


class TargetSelectionTests(unittest.TestCase):
    """Which markets are sent to the simulator (direct-odds priority)."""

    def _capture_payload(self, markets, direct_by_market, **kwargs):
        run = MagicMock(return_value=_bridge_response([]))
        with patch.object(simulator, "_runtime",
                          return_value=(Path("/fake/root"), Path("/fake/py"))), \
                patch.object(simulator, "_run_bridge", run):
            simulator.simulator_estimates(
                markets, _ctx(), direct_by_market=direct_by_market, **kwargs)
        if not run.call_args:
            return None
        return run.call_args.args[0]

    def test_only_no_direct_questions_sent_by_default(self):
        markets = [
            {"id": "no_direct", "question": "Will there be a corner before the first hydration break?"},
            {"id": "has_direct", "question": "Will the home team win?"},
        ]
        direct = {"no_direct": [], "has_direct": [{"probability": 0.5}]}
        payload = self._capture_payload(markets, direct)

        sent = {q["market_id"] for q in payload["questions"]}
        self.assertEqual(sent, {"no_direct"})

    def test_penalty_with_direct_odds_skips_simulator(self):
        markets = [{"id": "pen", "question": "Will a penalty kick be awarded in the match?"}]
        direct = {"pen": [{"probability": 0.27}]}  # has a direct price
        payload = self._capture_payload(markets, direct)

        self.assertIsNone(payload)

    def test_sot_with_direct_odds_skips_simulator(self):
        markets = [{"id": "sot", "question": "Will Home have more shots on target than Away in the second half?"}]
        intents = {"sot": {"market": "shots_on_target_compare", "subject": "home",
                           "comparator": "more", "threshold": None, "period": "2H"}}
        direct = {"sot": [{"probability": 0.52}]}
        payload = self._capture_payload(markets, direct, intents=intents)

        self.assertIsNone(payload)

    def test_all_markets_have_direct_and_none_model_sensitive_skips_bridge(self):
        markets = [{"id": "win", "question": "Will the home team win?"}]
        direct = {"win": [{"probability": 0.5}]}
        payload = self._capture_payload(markets, direct)
        # _run_bridge is never called -> no payload captured.
        self.assertIsNone(payload)

    def test_raw_lineups_kickoff_referee_stage_are_passed(self):
        markets = [{"id": "m1", "question": "Will a goal be scored before the first hydration break?"}]
        lineups = [{"team": {"name": "Brazil"}, "startXI": [{"player": {"name": "X", "pos": "F"}}]}]
        payload = self._capture_payload(
            markets, {"m1": []}, lineups=lineups, kickoff="2026-06-28T19:00:00Z",
            referee="P. Tierney", stage="knockout")

        self.assertEqual(payload["lineups"], lineups)
        self.assertEqual(payload["kickoff"], "2026-06-28T19:00:00Z")
        self.assertEqual(payload["referee"], "P. Tierney")
        self.assertEqual(payload["stage"], "knockout")
        self.assertEqual(payload["home"], "Brazil")
        self.assertEqual(payload["away"], "Japan")
        self.assertNotIn("market_odds", payload)

    def test_runtime_preserves_virtualenv_launcher(self):
        with patch.object(simulator.sys, "executable", "/tmp/example-venv/bin/python"), \
                patch.object(Path, "is_file", return_value=True):
            runtime = simulator._runtime(Path("/tmp/simulator"))
        self.assertEqual(runtime[1], Path("/tmp/example-venv/bin/python"))

    def test_lineups_key_absent_when_no_lineups(self):
        markets = [{"id": "m1", "question": "Will a goal be scored before the first hydration break?"}]
        payload = self._capture_payload(markets, {"m1": []})
        self.assertNotIn("lineups", payload)


class FailOpenTests(unittest.TestCase):
    """A missing runtime, timeout, nonzero exit or bad output must yield {}."""

    def test_missing_runtime_returns_empty(self):
        markets = [{"id": "m1", "question": "Will a goal be scored before the first hydration break?"}]
        out = simulator.simulator_estimates(
            markets, _ctx(), direct_by_market={"m1": []},
            simulator_root=Path("/does/not/exist/xyz"))
        self.assertEqual(out, {})

    def test_no_targets_returns_empty_without_touching_runtime(self):
        markets = [{"id": "win", "question": "Will the home team win?"}]
        with patch.object(simulator, "_runtime") as runtime:
            out = simulator.simulator_estimates(
                markets, _ctx(), direct_by_market={"win": [{"probability": 0.5}]})
        self.assertEqual(out, {})
        runtime.assert_not_called()

    def _run_bridge(self, proc_mock=None, side_effect=None):
        with patch.object(simulator.subprocess, "run",
                          MagicMock(return_value=proc_mock, side_effect=side_effect)):
            return simulator._run_bridge({"home": "A", "away": "B"},
                                            Path("/fake/root"), Path("/fake/py"))

    def test_timeout_returns_empty(self):
        out = self._run_bridge(
            side_effect=subprocess.TimeoutExpired(cmd="sphybrid", timeout=120))
        self.assertEqual(out, {})

    def test_nonzero_exit_returns_empty(self):
        proc = MagicMock(returncode=1, stdout="", stderr="boom")
        out = self._run_bridge(proc_mock=proc)
        self.assertEqual(out, {})

    def test_invalid_json_returns_empty(self):
        proc = MagicMock(returncode=0, stdout="this is not json", stderr="")
        out = self._run_bridge(proc_mock=proc)
        self.assertEqual(out, {})

    def test_success_returns_parsed_dict(self):
        resp = _bridge_response([_report("m1", "q", "goal_window", 0.4)])
        proc = MagicMock(returncode=0, stdout=json.dumps(resp), stderr="")
        out = self._run_bridge(proc_mock=proc)
        self.assertEqual(out["question_reports"][0]["market_id"], "m1")

    def test_run_bridge_sets_pythonpath_to_src(self):
        proc = MagicMock(returncode=0, stdout="{}", stderr="")
        captured = {}

        def fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            captured["env"] = kwargs.get("env")
            captured["cwd"] = kwargs.get("cwd")
            return proc

        with patch.object(simulator.subprocess, "run", fake_run):
            simulator._run_bridge({"home": "A"}, Path("/fake/root"), Path("/fake/py"))

        self.assertEqual(captured["cmd"][1:], ["-m", "sphybrid.bridge"])
        self.assertTrue(captured["env"]["PYTHONPATH"].startswith(str(Path("/fake/root/src"))))
        # Config and artifact resolution is pinned to the tracked runtime.
        self.assertEqual(captured["env"]["SPORTSPREDICT_ROOT"], str(Path("/fake/root")))
        self.assertEqual(captured["cwd"], str(Path("/fake/root")))


if __name__ == "__main__":
    unittest.main()
