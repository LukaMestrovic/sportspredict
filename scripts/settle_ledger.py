#!/usr/bin/env python3
"""Settle recorded real questions and report live prediction performance."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from bot import calibration, ledger
from bot.sportspredict import SportPredict
from bot.web import WebAPI


_SRC_TAG = {"api-football": "AF", "odds-api": "OA", "af+oa": "AF+OA",
            "derived": "DRV", "empirical": "EMP", "external": "WEB",
            "llm-pricing": "LLM"}


def _print_match_detail(detail: dict) -> None:
    """Per-question review: final probability, audit paths, outcome, reasoning."""
    run = detail["run"]
    print(f"\n=== {run['match_name']} ===")
    print(f"kickoff {run['kickoff']}  window={run['window_min']}min  "
          f"recorded {run['recorded_at']}  status={run['status']}")
    if run.get("evidence_path"):
        print(f"[evidence] {run['evidence_path']}  hash={run.get('evidence_hash')}")
    if run.get("llm_pricing_report_path"):
        print(f"[audit] {run['llm_pricing_report_path']}")
    briefing = run.get("llm_pricing_briefing_json") or run.get("calibration_briefing_json")
    if briefing:
        blob = json.loads(briefing)
        print(f"[briefing] {blob.get('briefing')}")
        if blob.get("sources"):
            print(f"[sources] {', '.join(blob['sources'][:5])}")
    print(
        f"\n{'raw':>5} {'final':>5} {'src':>6} {'n':>3} {'out':>4} "
        f"{'brier':>7}  question / rationale"
    )
    for q in detail["questions"]:
        if q["probability_int"] is None:
            continue  # skipped question
        prob = q["probability_int"]
        raw = q["raw_probability_int"] if q["raw_probability_int"] is not None else prob
        books = q["n_books"] or 0
        outcome = "·" if q["outcome"] is None else str(q["outcome"])
        brier = f"{q['brier_score']:.3f}" if q["brier_score"] is not None else "·"
        src = _SRC_TAG.get(q["source"], q["source"] or "?")
        print(
            f"{raw:>4}% {prob:>4}% {src:>6} {books:>3} {outcome:>4} "
            f"{brier:>7}  {q['question']}"
        )
        if q["calibration_gate_reason"]:
            print(
                f"{'':>38}  calibration: {q['calibration_gate_reason']} "
                f"(model={q['calibration_model_id'] or 'identity'})"
            )
        rationale = q["llm_reasoning_summary"] or q["calibration_rationale"]
        if rationale:
            print(f"{'':>38}  ↳ {rationale}")
        if q["llm_audit_json"]:
            audit = json.loads(q["llm_audit_json"])
            for label, key in (
                ("provided odds", "provided_odds_used"),
                ("online odds", "online_odds_found"),
                ("non-odds", "non_odds_factors_used"),
                ("downweighted", "ignored_or_downweighted_evidence"),
            ):
                items = audit.get(key) or []
                print(f"{'':>38}  {label}: {len(items)} item(s)")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--ledger", type=Path, default=ledger.LEDGER_PATH,
        help="SQLite ledger path",
    )
    parser.add_argument(
        "--match", default=None,
        help="review one match by id or name substring (local read, no settle)",
    )
    args = parser.parse_args()

    if args.match:
        detail = ledger.match_detail(args.match, path=args.ledger)
        if not detail:
            print(f"No submitted run found for match {args.match!r}.")
            return
        _print_match_detail(detail)
        return

    sp = SportPredict()
    event = sp.event()
    lobby = sp.lobby(event["id"])
    sync = calibration.sync_and_refit(
        sp, WebAPI(), event, lobby, path=args.ledger,
    )
    print(
        f"Calibration sync: {sync['observations_imported']} new observations, "
        f"{sync['observations_total']} total across {sync['usable_matches']} matches; "
        f"refit={sync['refit']}."
    )
    for row in ledger.performance(path=args.ledger):
        if row["group"] == "overall":
            label = "overall"
        elif row["group"] == "window":
            label = f"window={row['window_min']}min"
        else:
            label = f"source={row['source']}"
        extra = ""
        if row.get("mean_raw_brier") is not None:
            extra += f"  (raw={row['mean_raw_brier']:.4f})"
        if row.get("mean_anchor_brier") is not None and row.get("tilted"):
            extra += (f"  (legacy-anchor={row['mean_anchor_brier']:.4f}, "
                      f"{row['tilted']} tilted)")
        print(
            f"  {label:<24} n={row['predictions']:>4}  "
            f"mean Brier={row['mean_brier']:.4f}{extra}"
        )


if __name__ == "__main__":
    main()
