#!/usr/bin/env python3
"""Price the next N open matches and log every question and prediction.

Uses the configured auditable LLM pricing layer. Does not submit by itself; pass
--submit to send the logged predictions to SportPredict after review.
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from bot.apifootball import APIFootball
from bot.oddsapi import OddsAPI
from bot.pipeline import run_match, submit_with_ledger
from bot.sportspredict import SportPredict

TAG = {"api-football": "AF", "odds-api": "OA", "derived": "DRV",
       "empirical": "EMP", "external": "WEB", "llm-pricing": "LLM"}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=6)
    ap.add_argument("--submit", action="store_true")
    args = ap.parse_args()

    sp = SportPredict()
    af = APIFootball()
    oa = OddsAPI()
    event = sp.event()
    lobby = sp.lobby(event["id"])
    matches = sp.matches(event["id"], lobby["id"])[: args.limit]

    results = []
    log = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "event": event.get("title"),
        "lobby_id": lobby["id"],
        "matches": [],
    }

    md = ["# Predictions log", "",
          f"generated {log['generated_at']}  |  lobby {lobby['id']}", ""]

    for sp_match in matches:
        markets = sp.markets(lobby["id"], sp_match["id"])
        r = run_match(sp_match, markets, af, oa)
        results.append(r)
        head = f"{r.home} vs {r.away}" if r.home else sp_match["name"]
        kickoff = sp_match["opening_time"]

        m_entry = {"match": head, "kickoff": kickoff,
                   "evidence_path": r.evidence_path,
                   "llm_pricing_report_path": r.llm_pricing_report_path,
                   "predictions": [], "skipped": []}
        md.append(f"## {head}  ({kickoff})")
        if r.evidence_path:
            md.append(f"evidence: `{r.evidence_path}`")
        if r.llm_pricing_report_path:
            md.append(f"audit: `{r.llm_pricing_report_path}`")
        md.append("")
        md.append("| prob | src | books | question |")
        md.append("|---:|---|---:|---|")
        for p in sorted(r.predictions, key=lambda x: x.source):
            tag = TAG.get(p.source, "??")
            md.append(f"| {p.probability_int}% | {tag} | {p.n_books or ''} "
                      f"| {p.question} |")
            m_entry["predictions"].append({
                "question": p.question,
                "probability_int": p.probability_int,
                "probability": round(p.probability, 4),
                "source": p.source,
                "n_books": p.n_books,
                "market_id": p.market_id,
                "label": p.market_label,
                "audit": p.llm_audit,
            })
        for q, why in r.skipped:
            md.append(f"| — | skip | | {q} ({why}) |")
            m_entry["skipped"].append({"question": q, "why": why})
        md.append("")
        log["matches"].append(m_entry)

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    log_dir = Path("logs")
    log_dir.mkdir(exist_ok=True)
    json_path = log_dir / f"predictions_{stamp}.json"
    md_path = log_dir / f"predictions_{stamp}.md"
    with json_path.open("w") as f:
        json.dump(log, f, indent=2)
    with md_path.open("w") as f:
        f.write("\n".join(md))

    # console summary
    by_src: dict[str, int] = {}
    total_pred = total_skip = 0
    for r in results:
        for p in r.predictions:
            by_src[p.source] = by_src.get(p.source, 0) + 1
        total_pred += len(r.predictions)
        total_skip += len(r.skipped)
    print(f"\nmatches: {len(results)}  predicted: {total_pred}  "
          f"skipped: {total_skip}")
    print("by source:", by_src)
    print("log:", md_path, "|", json_path)

    if args.submit:
        outcome, _run_ids = submit_with_ledger(
            sp, event["id"], lobby["id"], results,
        )
        landed = outcome["submitted"] + outcome["updated"] + outcome["unchanged"]
        print(f"UPSERT: created={outcome['submitted']} updated={outcome['updated']} "
              f"unchanged={outcome['unchanged']} failed={outcome['failed']} "
              f"(landed={landed}/{len(outcome['payload'])})")


if __name__ == "__main__":
    main()
