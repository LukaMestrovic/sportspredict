#!/usr/bin/env python3
"""CLI entry point for the SportPredict probability bot.

Usage:
  python run.py predict            # predict all open matches, print results
  python run.py predict --submit   # ... and submit them to SportPredict
  python run.py predict --limit 1  # only the first open match (cheap test)
  python run.py predict --limit 1 --no-llm     # deterministic backtest-style preview
"""
import argparse

from bot.pipeline import predict_open_matches


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("predict", help="predict open matches")
    p.add_argument("--submit", action="store_true", help="submit to SportPredict")
    p.add_argument("--limit", type=int, default=None, help="limit number of matches")
    p.add_argument("--no-llm", action="store_true",
                   help="disable final LLM pricing and use deterministic cascade")
    args = ap.parse_args()

    if args.cmd == "predict":
        results = predict_open_matches(
            submit=args.submit, limit=args.limit,
            llm_pricing_enabled=not args.no_llm,
        )
        total_pred = total_skip = 0
        for r in results:
            head = f"{r.home} vs {r.away}" if r.home else r.sp_match["name"]
            print(f"\n=== {head} ===")
            if r.evidence_path:
                print(f"  [evidence] {r.evidence_path}")
            if r.llm_pricing_report_path:
                print(f"  [audit] {r.llm_pricing_report_path}")
            for p in r.predictions:
                tag = {"api-football": "AF", "odds-api": "OA", "af+oa": "AF+OA",
                       "derived": "DRV", "empirical": "EMP",
                       "llm-pricing": "LLM"}.get(p.source, "??")
                books = f" {p.n_books}b" if p.n_books else ""
                print(f"  {p.probability_int:>5}%  [{tag}{books}] {p.question}")
                if p.llm_reasoning_summary:
                    print(f"        ↳ {p.llm_reasoning_summary}")
            for q, why in r.skipped:
                print(f"   --  (skip: {why}) {q}")
            if r.llm_pricing_briefing:
                print(f"  [briefing] {r.llm_pricing_briefing}")
                if r.llm_pricing_sources:
                    print(f"  [sources] {', '.join(r.llm_pricing_sources[:5])}")
            total_pred += len(r.predictions)
            total_skip += len(r.skipped)
        print(f"\nTotal: {total_pred} predicted, {total_skip} skipped"
              + (" — SUBMITTED" if args.submit else ""))


if __name__ == "__main__":
    main()
