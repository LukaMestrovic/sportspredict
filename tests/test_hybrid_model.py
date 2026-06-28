"""Tests for the sportspredict-hybrid simulation-report bridge."""
import json
import subprocess
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from bot import hybrid_model
from bot.pricing import PriceCtx


def _ctx():
    return PriceCtx("Brazil", "Japan", af_books=[], oa=None, oa_event=None)


def _bridge_response(reports, *, unsupported=None, model=None):
    return {
        "schema_version": "1.0",
        "match": {"team_a": "Brazil", "team_b": "Japan", "stage": "group"},
        "model": model or {
            "engine": "HybridEngine", "rate_model": "LearnedRateModel",
            "n_sims": 8000, "odds_anchor_applied": True,
        },
        "evidence_instruction": "Model context only: weigh against odds…",
        "question_reports": reports,
        "unsupported_questions": unsupported or [],
    }


def _report(market_id, question, family, probability):
    return {
        "market_id": market_id,
        "question": question,
        "source": "sportspredict-hybrid",
        "family": family,
        "probability": probability,
        "probability_pct": round(probability * 100, 2),
        "explanation": f"Deterministic basis for {family}.",
        "evidence_role": "model_context",
    }


class ReportParsingTests(unittest.TestCase):
    """Mapping of a successful report into per-market estimate items."""

    def _run(self, markets, direct_by_market, bridge_response, **kwargs):
        run = MagicMock(return_value=bridge_response)
        with patch.object(hybrid_model, "_sibling",
                          return_value=(Path("/fake/root"), Path("/fake/py"))), \
                patch.object(hybrid_model, "_run_bridge", run):
            out = hybrid_model.simulator_estimates(
                markets, _ctx(), direct_by_market=direct_by_market, **kwargs)
        return out, run

    def test_successful_report_is_keyed_by_market_id(self):
        markets = [{"id": "m1", "question": "Will a goal be scored before the first hydration break?"}]
        resp = _bridge_response([_report("m1", markets[0]["question"], "goal_window", 0.402)])
        out, _ = self._run(markets, {"m1": []}, resp)

        self.assertIn("m1", out)
        item = out["m1"]
        self.assertEqual(item["source"], "sportspredict-hybrid")
        self.assertEqual(item["family"], "goal_window")
        self.assertEqual(item["probability"], 0.402)
        self.assertEqual(item["probability_pct"], 40.2)
        self.assertEqual(item["evidence_role"], "model_context")
        self.assertIn("Deterministic basis", item["explanation"])
        self.assertEqual(item["model"]["rate_model"], "LearnedRateModel")
        self.assertEqual(item["model"]["n_sims"], 8000)
        self.assertTrue(item["model"]["odds_anchor_applied"])
        self.assertIn("not a final anchor", item["note"])

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
            ("any_sot", "Will any player have 3 or more shots on target?", "any_player_threshold"),
            ("any_brace", "Will any player score a brace?", "any_player_threshold"),
        ]
        markets = [{"id": mid, "question": q} for mid, q, _ in families]
        reports = [_report(mid, q, fam, 0.3) for mid, q, fam in families]
        out, _ = self._run(markets, {mid: [] for mid, _, _ in families},
                           _bridge_response(reports))

        for mid, _q, fam in families:
            self.assertIn(mid, out, f"{mid} missing from estimates")
            self.assertEqual(out[mid]["family"], fam)


class TargetSelectionTests(unittest.TestCase):
    """Which markets are sent to the simulator (direct-odds priority)."""

    def _capture_payload(self, markets, direct_by_market, **kwargs):
        run = MagicMock(return_value=_bridge_response([]))
        with patch.object(hybrid_model, "_sibling",
                          return_value=(Path("/fake/root"), Path("/fake/py"))), \
                patch.object(hybrid_model, "_run_bridge", run):
            hybrid_model.simulator_estimates(
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

    def test_model_sensitive_penalty_sent_even_with_direct_odds(self):
        markets = [{"id": "pen", "question": "Will a penalty kick be awarded in the match?"}]
        direct = {"pen": [{"probability": 0.27}]}  # has a direct price
        payload = self._capture_payload(markets, direct)

        sent = {q["market_id"] for q in payload["questions"]}
        self.assertIn("pen", sent)

    def test_model_sensitive_sot_sent_even_with_direct_odds(self):
        markets = [{"id": "sot", "question": "Will Home have more shots on target than Away in the second half?"}]
        intents = {"sot": {"market": "shots_on_target_compare", "subject": "home",
                           "comparator": "more", "threshold": None, "period": "2H"}}
        direct = {"sot": [{"probability": 0.52}]}
        payload = self._capture_payload(markets, direct, intents=intents)

        self.assertIn("sot", {q["market_id"] for q in payload["questions"]})

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

    def test_lineups_key_absent_when_no_lineups(self):
        markets = [{"id": "m1", "question": "Will a goal be scored before the first hydration break?"}]
        payload = self._capture_payload(markets, {"m1": []})
        self.assertNotIn("lineups", payload)


class FailOpenTests(unittest.TestCase):
    """A missing sibling, timeout, nonzero exit or bad output must yield {}."""

    def test_missing_sibling_returns_empty(self):
        markets = [{"id": "m1", "question": "Will a goal be scored before the first hydration break?"}]
        with patch.object(hybrid_model, "_hybrid_python", return_value=None):
            out = hybrid_model.simulator_estimates(
                markets, _ctx(), direct_by_market={"m1": []},
                hybrid_root=Path("/does/not/exist/xyz"))
        self.assertEqual(out, {})

    def test_no_targets_returns_empty_without_touching_sibling(self):
        markets = [{"id": "win", "question": "Will the home team win?"}]
        with patch.object(hybrid_model, "_sibling") as sibling:
            out = hybrid_model.simulator_estimates(
                markets, _ctx(), direct_by_market={"win": [{"probability": 0.5}]})
        self.assertEqual(out, {})
        sibling.assert_not_called()

    def _run_bridge(self, proc_mock=None, side_effect=None):
        with patch.object(hybrid_model.subprocess, "run",
                          MagicMock(return_value=proc_mock, side_effect=side_effect)):
            return hybrid_model._run_bridge({"home": "A", "away": "B"},
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

        with patch.object(hybrid_model.subprocess, "run", fake_run):
            hybrid_model._run_bridge({"home": "A"}, Path("/fake/root"), Path("/fake/py"))

        self.assertEqual(captured["cmd"][1:], ["-m", "sphybrid.cli", "simulation-report"])
        self.assertTrue(captured["env"]["PYTHONPATH"].startswith(str(Path("/fake/root/src"))))
        # SPORTSPREDICT_ROOT must be pinned to the sibling root so the report's
        # config/artifact resolution is deterministic in the deployed image.
        self.assertEqual(captured["env"]["SPORTSPREDICT_ROOT"], str(Path("/fake/root")))
        self.assertEqual(captured["cwd"], str(Path("/fake/root")))


if __name__ == "__main__":
    unittest.main()
