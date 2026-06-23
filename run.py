#!/usr/bin/env python3
"""CLI entry point for the SportPredict probability bot.

Usage:
  python run.py predict            # predict all open matches, print results
  python run.py predict --submit   # ... and submit them to SportPredict
  python run.py predict --limit 1  # only the first open match (cheap test)
  python run.py predict --limit 1 --calibrate  # preview the LLM calibration layer
"""
import argparse

from bot import calibrate
from bot.pipeline import predict_open_matches


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("predict", help="predict open matches")
    p.add_argument("--submit", action="store_true", help="submit to SportPredict")
    p.add_argument("--limit", type=int, default=None, help="limit number of matches")
    p.add_argument("--calibrate", action="store_true",
                   help="preview the LLM calibration layer (tilts anchors, no submit)")
    args = ap.parse_args()

    if args.cmd == "predict":
        results = predict_open_matches(
            submit=args.submit, limit=args.limit, calibrate_layer=args.calibrate
        )
        total_pred = total_skip = total_tilted = 0
        for r in results:
            head = f"{r.home} vs {r.away}" if r.home else r.sp_match["name"]
            print(f"\n=== {head} ===")
            for p in r.predictions:
                tag = {"api-football": "AF", "odds-api": "OA", "af+oa": "AF+OA",
                       "derived": "DRV", "empirical": "EMP",
                       "external": "WEB"}.get(p.source, "??")
                books = f" {p.n_books}b" if p.n_books else ""
                print(f"  {p.probability_int:>2}%  [{tag}{books}] {p.question}")
                if args.calibrate and p.applied_delta:
                    print(f"        ↳ anchor {p.anchor_probability_int}% → "
                          f"{p.probability_int}%  (tilt {p.tilt_points:+g}, "
                          f"cap ±{calibrate.cap_for_books(p.n_books or 0)})  "
                          f"{p.calibration_rationale or ''}")
                    total_tilted += 1
            for q, why in r.skipped:
                print(f"   --  (skip: {why}) {q}")
            if args.calibrate and r.calibration_briefing:
                print(f"  [briefing] {r.calibration_briefing}")
                if r.calibration_sources:
                    print(f"  [sources] {', '.join(r.calibration_sources[:5])}")
            total_pred += len(r.predictions)
            total_skip += len(r.skipped)
        tail = f", {total_tilted} tilted" if args.calibrate else ""
        print(f"\nTotal: {total_pred} predicted, {total_skip} skipped{tail}"
              + (" — SUBMITTED" if args.submit else ""))


if __name__ == "__main__":
    main()
