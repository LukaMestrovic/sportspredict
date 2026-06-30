#!/usr/bin/env python3
"""Settle recorded real questions and report live prediction performance."""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from bot import ledger, simulator_benchmark, wc2026_evidence
from bot.apifootball import APIFootball
from bot.sportspredict import SportPredict
from bot.web import WebAPI


_SRC_TAG = {"api-football": "AF", "odds-api": "OA", "af+oa": "AF+OA",
            "derived": "DRV", "empirical": "EMP",
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
    briefing = run.get("llm_pricing_briefing_json")
    if briefing:
        blob = json.loads(briefing)
        print(f"[briefing] {blob.get('briefing')}")
        if blob.get("sources"):
            print(f"[sources] {', '.join(blob['sources'][:5])}")
    print(
        f"\n{'prob':>5} {'src':>6} {'n':>3} {'out':>4} "
        f"{'brier':>7}  question / rationale"
    )
    for q in detail["questions"]:
        if q["probability_int"] is None:
            continue  # skipped question
        prob = q["probability_int"]
        books = q["n_books"] or 0
        outcome = "·" if q["outcome"] is None else str(q["outcome"])
        brier = f"{q['brier_score']:.3f}" if q["brier_score"] is not None else "·"
        src = _SRC_TAG.get(q["source"], q["source"] or "?")
        print(
            f"{prob:>4}% {src:>6} {books:>3} {outcome:>4} "
            f"{brier:>7}  {q['question']}"
        )
        rationale = q["llm_reasoning_summary"]
        if rationale:
            print(f"{'':>30}  ↳ {rationale}")
        if q["llm_audit_json"]:
            audit = json.loads(q["llm_audit_json"])
            for label, key in (
                ("provided odds", "provided_odds_used"),
                ("online odds", "online_odds_found"),
                ("non-odds", "non_odds_factors_used"),
                ("downweighted", "ignored_or_downweighted_evidence"),
            ):
                items = audit.get(key) or []
                print(f"{'':>30}  {label}: {len(items)} item(s)")


def settle_open(path: Path = ledger.LEDGER_PATH) -> tuple[dict, dict]:
    """Settle explicit outcomes and refresh tournament empirical/simulator evidence."""
    sp = SportPredict()
    event = sp.event()
    lobby = sp.lobby(event["id"])
    web = WebAPI()

    match_ids = ledger.unsettled_match_ids(lobby["id"], path=path)
    results = sp.results(lobby["id"]) if match_ids else []
    outcomes: dict[str, int] = {}
    for match_id in match_ids:
        for market in web.crowd_stats(match_id, lobby["id"]):
            value = market.get("current_value")
            if value in (0, 100):
                outcomes[market["id"]] = value // 100
    stats = ledger.settle_results(outcomes, results, path=path)
    snapshot = simulator_benchmark.refresh(
        sp, web, event["id"], lobby["id"],
    )
    empirical = wc2026_evidence.refresh(
        APIFootball(refresh_odds=True),
        datetime.now(timezone.utc).isoformat(),
        wc2026_evidence.known_contract_keys() | set(snapshot.get("contracts") or {}),
    )
    snapshot["empirical_refresh"] = {
        key: empirical.get(key)
        for key in (
            "generated_at", "eligible_matches", "covered_matches",
            "complete", "data_through",
        )
    }
    return stats, snapshot


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

    stats, benchmark = settle_open(args.ledger)
    print(
        f"Settled {stats['settled_predictions']} predictions; "
        f"{stats['remaining_predictions']} still open."
    )
    print(
        f"WC2026 simulator benchmark: {benchmark['comparable_simulator_questions']} "
        f"comparable questions across {benchmark['replayed_matches']} replayed matches "
        f"({benchmark['settled_tournament_matches']} settled)."
    )
    for row in ledger.performance(path=args.ledger):
        if row["group"] == "overall":
            label = "overall"
        elif row["group"] == "window":
            label = f"window={row['window_min']}min"
        else:
            label = f"source={row['source']}"
        print(
            f"  {label:<24} n={row['predictions']:>4}  "
            f"mean Brier={row['mean_brier']:.4f}"
        )


if __name__ == "__main__":
    main()
