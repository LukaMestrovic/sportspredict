#!/usr/bin/env python3
"""Settle recorded real questions and report live prediction performance."""
from __future__ import annotations

import argparse
from pathlib import Path

from bot import ledger
from bot.sportspredict import SportPredict
from bot.web import WebAPI


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--ledger", type=Path, default=ledger.LEDGER_PATH,
        help="SQLite ledger path",
    )
    args = parser.parse_args()

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
        print(
            f"  {label:<24} n={row['predictions']:>4}  "
            f"mean Brier={row['mean_brier']:.4f}"
        )


if __name__ == "__main__":
    main()
