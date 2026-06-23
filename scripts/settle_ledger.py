#!/usr/bin/env python3
"""Settle recorded real questions and report live prediction performance."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from statistics import pstdev

from bot import ledger
from bot.sportspredict import SportPredict
from bot.web import WebAPI


_SRC_TAG = {"api-football": "AF", "odds-api": "OA", "af+oa": "AF+OA",
            "derived": "DRV", "empirical": "EMP", "external": "WEB"}


def _print_match_detail(detail: dict) -> None:
    """Per-question review: anchor → calibrated, tilt, outcome, reasoning."""
    run = detail["run"]
    print(f"\n=== {run['match_name']} ===")
    print(f"kickoff {run['kickoff']}  window={run['window_min']}min  "
          f"recorded {run['recorded_at']}  status={run['status']}")
    briefing = run.get("calibration_briefing_json")
    if briefing:
        blob = json.loads(briefing)
        print(f"[briefing] {blob.get('briefing')}")
        if blob.get("sources"):
            print(f"[sources] {', '.join(blob['sources'][:5])}")
    print(f"\n{'anchor→cal':>11} {'tilt':>5} {'src':>6} {'n':>3} "
          f"{'spread':>7} {'out':>4} {'aBr→cBr':>13}  question / rationale")
    for q in detail["questions"]:
        if q["probability_int"] is None:
            continue  # skipped question
        anchor = q["anchor_probability_int"]
        prob = q["probability_int"]
        move = (f"{anchor}→{prob}" if anchor is not None and anchor != prob
                else f"{prob}")
        tilt = q["tilt_points"]
        tilt_s = f"{tilt:+g}" if tilt else "·"
        books = q["n_books"] or 0
        bp = json.loads(q["book_probabilities_json"] or "[]")
        spread = f"{pstdev(bp):.3f}" if len(bp) >= 2 else "·"
        outcome = "·" if q["outcome"] is None else str(q["outcome"])
        if q["anchor_brier_score"] is not None and q["brier_score"] is not None:
            briers = f"{q['anchor_brier_score']:.3f}→{q['brier_score']:.3f}"
        elif q["brier_score"] is not None:
            briers = f"{q['brier_score']:.3f}"
        else:
            briers = "·"
        src = _SRC_TAG.get(q["source"], q["source"] or "?")
        print(f"{move:>11} {tilt_s:>5} {src:>6} {books:>3} "
              f"{spread:>7} {outcome:>4} {briers:>13}  {q['question']}")
        if q["calibration_rationale"]:
            print(f"{'':>53}  ↳ {q['calibration_rationale']}")


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
    match_ids = ledger.unsettled_match_ids(lobby["id"], path=args.ledger)
    results = sp.results(lobby["id"])

    web = WebAPI()
    settled_match_ids = {
        match["id"] for match in web.settled_matches(event["id"], limit=200)
    }
    outcomes: dict[str, int] = {}
    for match_id in match_ids:
        if match_id not in settled_match_ids:
            continue
        for market in web.crowd_stats(match_id, lobby["id"]):
            value = market.get("current_value")
            if value in (0, 100):
                outcomes[market["id"]] = value // 100

    stats = ledger.settle_results(outcomes, results, path=args.ledger)
    print(
        f"Settled {stats['settled_predictions']} predictions; "
        f"{stats['remaining_predictions']} remain open."
    )
    for row in ledger.performance(path=args.ledger):
        if row["group"] == "overall":
            label = "overall"
        elif row["group"] == "window":
            label = f"window={row['window_min']}min"
        else:
            label = f"source={row['source']}"
        extra = ""
        if row.get("mean_anchor_brier") is not None and row.get("tilted"):
            extra = (f"  (anchor={row['mean_anchor_brier']:.4f}, "
                     f"{row['tilted']} tilted)")
        print(
            f"  {label:<24} n={row['predictions']:>4}  "
            f"mean Brier={row['mean_brier']:.4f}{extra}"
        )


if __name__ == "__main__":
    main()
