"""Bot-vs-crowd edge by market family (settled-market post-mortem).

Scores the bot *as if it had been deployed the whole time*: for every settled
Probability Cup match the web API exposes, it re-prices the markets through the
production cascade (API-Football -> Odds API -> derive; web layer OFF to avoid
leaking past results), joins each priced market to the crowd mean and realized
outcome, and reports the **edge over the crowd** per market family.

Edge = crowd Brier - bot Brier  (positive => the bot beats the crowd).

    python -m analysis.bot_vs_crowd_edge [--limit N] [--png PATH]

Writes a per-family edge bar chart (matplotlib) and prints the same table.
"""
from __future__ import annotations

import argparse
import csv

from bot.apifootball import APIFootball
from bot.oddsapi import OddsAPI
from bot.pipeline import run_match
from bot.sportspredict import SportPredict
from bot.web import WebAPI

# Market key (parser intent) -> human market family. Anything unseen, plus
# compounds (intent "none"), falls back to FAMILY_OTHER.
_FAMILY = {
    "match_winner": "Match result",
    "match_draw": "Match result",
    "double_chance": "Match result",
    "highest_scoring_half_2h": "Match result",
    "first_team_to_score": "Match result",
    "btts": "Goals / BTTS",
    "total_goals": "Goals / BTTS",
    "team_total_goals": "Goals / BTTS",
    "team_score": "Goals / BTTS",
    "team_score_1h": "Goals / BTTS",
    "team_score_2h": "Goals / BTTS",
    "total_corners": "Corners",
    "team_corners": "Corners",
    "corners_compare": "Corners",
    "total_cards": "Cards",
    "team_cards": "Cards",
    "cards_compare": "Cards",
    "total_offsides": "Offsides",
    "team_offsides": "Offsides",
    "offsides_compare": "Offsides",
    "total_fouls": "Fouls",
    "team_fouls": "Fouls",
    "fouls_compare": "Fouls",
    "total_shots_on_target": "Shots on target",
    "team_shots_on_target": "Shots on target",
    "shots_on_target_compare": "Shots on target",
    "player_shots_on_target": "Player props",
    "player_goal_scorer": "Player props",
    "player_score_or_assist": "Player props",
    "player_card": "Player props",
}
FAMILY_COMPOUND = "Compound (derived)"
FAMILY_OTHER = "Other"


def family_for(intent: dict | None, source: str) -> str:
    market = (intent or {}).get("market")
    if not market or market == "none":
        # Parser declined (compound/unsupported); the cascade derived it.
        return FAMILY_COMPOUND
    return _FAMILY.get(market, FAMILY_OTHER)


def collect(limit: int) -> list[dict]:
    sp, web, af, oa = SportPredict(), WebAPI(), APIFootball(), OddsAPI()
    event = sp.event()
    lobby = sp.lobby(event["id"])
    print(f"event: {event['title']} | lobby: {lobby['id']}")

    settled = web.settled_matches(event["id"], limit=limit)
    print(f"settled matches available: {len(settled)}\n")

    rows: list[dict] = []
    priced_matches = 0
    for sm in settled:
        try:
            crowd = web.crowd_stats(sm["id"], lobby["id"])
            # web layer OFF: a web search on a past match could leak its result.
            res = run_match(sm, crowd, af, oa, allow_external=False)
        except Exception as e:  # keep going across the whole settled history
            print(f"{sm['name']:<24} ERROR {type(e).__name__}: {e}")
            continue
        bot = {p.market_id: p for p in res.predictions}
        n_here = 0
        for c in crowd:
            p = bot.get(c["id"])
            if p is None or c["current_value"] not in (0, 100):
                continue
            outcome = c["current_value"] // 100
            rows.append({
                "match": sm["name"],
                "question": c["question"],
                "family": family_for(res.intents.get(c["id"]), p.source),
                "source": p.source,
                "bot": p.probability,
                "crowd": c["prediction_average"] / 100.0,
                "outcome": outcome,
                "brier_bot": (p.probability - outcome) ** 2,
                "brier_crowd": (c["prediction_average"] / 100.0 - outcome) ** 2,
            })
            n_here += 1
        if n_here:
            priced_matches += 1
        print(f"{sm['name']:<24} head-to-head {n_here:>3}  (bot priced {len(bot)}/{len(crowd)})")
    print(f"\nmatches with >=1 head-to-head question: {priced_matches}/{len(settled)}")
    return rows


def summarize(rows: list[dict]) -> list[dict]:
    fams: dict[str, list[dict]] = {}
    for r in rows:
        fams.setdefault(r["family"], []).append(r)
    table = []
    for fam, rs in fams.items():
        n = len(rs)
        bb = sum(r["brier_bot"] for r in rs) / n
        bc = sum(r["brier_crowd"] for r in rs) / n
        wins = sum(1 for r in rs if r["brier_bot"] < r["brier_crowd"])
        table.append({
            "family": fam, "n": n,
            "brier_bot": bb, "brier_crowd": bc,
            "edge": bc - bb,            # positive => bot beats crowd
            "win_rate": wins / n,
        })
    table.sort(key=lambda t: t["edge"], reverse=True)
    return table


def print_table(table: list[dict], rows: list[dict]) -> None:
    print("\n" + "=" * 78)
    print(f"{'market family':<22}{'n':>5}{'bot Brier':>11}{'crowd Brier':>13}{'edge':>9}{'win%':>7}")
    print("-" * 78)
    for t in table:
        print(f"{t['family']:<22}{t['n']:>5}{t['brier_bot']:>11.4f}"
              f"{t['brier_crowd']:>13.4f}{t['edge']:>+9.4f}{t['win_rate']*100:>6.0f}%")
    n = len(rows)
    bb = sum(r["brier_bot"] for r in rows) / n
    bc = sum(r["brier_crowd"] for r in rows) / n
    wins = sum(1 for r in rows if r["brier_bot"] < r["brier_crowd"])
    print("-" * 78)
    print(f"{'OVERALL':<22}{n:>5}{bb:>11.4f}{bc:>13.4f}{bc-bb:>+9.4f}{wins/n*100:>6.0f}%")
    print("=" * 78)
    print("edge = crowd Brier - bot Brier  (positive => bot beats the crowd)")


def make_png(table: list[dict], path: str) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fams = [t["family"] for t in table]
    edges = [t["edge"] for t in table]
    ns = [t["n"] for t in table]
    colors = ["#2ca25f" if e >= 0 else "#de2d26" for e in edges]

    fig, ax = plt.subplots(figsize=(9, 0.55 * len(fams) + 1.6))
    bars = ax.barh(range(len(fams)), edges, color=colors)
    ax.set_yticks(range(len(fams)))
    ax.set_yticklabels([f"{f}  (n={n})" for f, n in zip(fams, ns)])
    ax.invert_yaxis()
    ax.axvline(0, color="#333", lw=1)
    ax.set_xlabel("edge over crowd  =  crowd Brier - bot Brier   (>0: bot wins)")
    ax.set_title("Bot edge over the crowd by market family (settled markets)")
    for b, e in zip(bars, edges):
        ax.text(e + (0.0008 if e >= 0 else -0.0008), b.get_y() + b.get_height() / 2,
                f"{e:+.3f}", va="center",
                ha="left" if e >= 0 else "right", fontsize=8)
    pad = max(abs(min(edges)), abs(max(edges)), 0.01) * 1.35
    ax.set_xlim(-pad, pad)
    plt.tight_layout()
    fig.savefig(path, dpi=130)
    print(f"\nsaved chart -> {path}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=200,
                    help="max settled matches to pull (default: all)")
    ap.add_argument("--png", default="analysis/bot_vs_crowd_edge.png")
    ap.add_argument("--csv", default="analysis/bot_vs_crowd_rows.csv",
                    help="dump per-question rows here for offline diagnosis")
    args = ap.parse_args()

    rows = collect(args.limit)
    if not rows:
        print("no head-to-head questions priced (pre-match odds likely purged).")
        return
    if args.csv:
        with open(args.csv, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)
        print(f"dumped {len(rows)} rows -> {args.csv}")
    table = summarize(rows)
    print_table(table, rows)
    try:
        make_png(table, args.png)
    except Exception as e:
        print(f"(skipped chart: {type(e).__name__}: {e})")


if __name__ == "__main__":
    main()
