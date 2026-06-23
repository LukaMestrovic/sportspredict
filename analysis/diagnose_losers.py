"""Diagnose WHY the bot loses to the crowd on specific market families.

Reads the row dump from `analysis.bot_vs_crowd_edge` and, per family, reports:
  - calibration bias: mean bot prob vs realized rate (over-/under-confident?)
  - whether the crowd is systematically biased the same way (can we lean on it?)
  - error split by pricing source
  - the worst individual misses

    python -m analysis.diagnose_losers [family ...]
"""
from __future__ import annotations

import csv
import sys
from collections import defaultdict

CSV = "analysis/bot_vs_crowd_rows.csv"
LOSERS = ["Cards", "Player props", "Compound (derived)", "Corners"]


def load() -> list[dict]:
    out = []
    with open(CSV) as f:
        for r in csv.DictReader(f):
            for k in ("bot", "crowd", "outcome", "brier_bot", "brier_crowd"):
                r[k] = float(r[k])
            out.append(r)
    return out


def fam_report(fam: str, rows: list[dict]) -> None:
    rs = [r for r in rows if r["family"] == fam]
    if not rs:
        print(f"\n### {fam}: no rows\n")
        return
    n = len(rs)
    mean_bot = sum(r["bot"] for r in rs) / n
    mean_crowd = sum(r["crowd"] for r in rs) / n
    rate = sum(r["outcome"] for r in rs) / n
    bb = sum(r["brier_bot"] for r in rs) / n
    bc = sum(r["brier_crowd"] for r in rs) / n
    # signed error = predicted - outcome; positive => over-predicted YES
    bot_bias = sum(r["bot"] - r["outcome"] for r in rs) / n
    crowd_bias = sum(r["crowd"] - r["outcome"] for r in rs) / n
    # how far bot sits from crowd, signed
    vs_crowd = sum(r["bot"] - r["crowd"] for r in rs) / n

    print(f"\n### {fam}  (n={n})")
    print(f"  realized YES rate : {rate:.3f}")
    print(f"  mean bot prob     : {mean_bot:.3f}   bias(bot-out)  : {bot_bias:+.3f}")
    print(f"  mean crowd prob   : {mean_crowd:.3f}   bias(crowd-out): {crowd_bias:+.3f}")
    print(f"  mean bot - crowd  : {vs_crowd:+.3f}")
    print(f"  Brier  bot {bb:.4f}  crowd {bc:.4f}  edge {bc-bb:+.4f}")

    # by source
    bysrc = defaultdict(list)
    for r in rs:
        bysrc[r["source"]].append(r)
    print("  by source:")
    for src, srs in sorted(bysrc.items(), key=lambda kv: -len(kv[1])):
        m = len(srs)
        sbb = sum(x["brier_bot"] for x in srs) / m
        sbc = sum(x["brier_crowd"] for x in srs) / m
        sbias = sum(x["bot"] - x["outcome"] for x in srs) / m
        print(f"    {src:<26} n={m:<3} bot {sbb:.3f}  crowd {sbc:.3f}  "
              f"edge {sbc-sbb:+.3f}  bias {sbias:+.3f}")

    # worst misses (where bot lost most to crowd)
    rs.sort(key=lambda r: r["brier_bot"] - r["brier_crowd"], reverse=True)
    print("  worst losses to crowd:")
    for r in rs[:6]:
        print(f"    out={int(r['outcome'])} bot={r['bot']:.2f} crowd={r['crowd']:.2f}"
              f"  {r['match']:<18} {r['question'][:64]}")


def main() -> None:
    rows = load()
    fams = sys.argv[1:] or LOSERS
    for fam in fams:
        fam_report(fam, rows)


if __name__ == "__main__":
    main()
