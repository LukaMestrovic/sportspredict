#!/usr/bin/env python3
"""End-to-end validation against settled matches from the last N days.

For every FINISHED WC2026 fixture in the window we:
  1. reconstruct the SportPredict-style questions for that match,
  2. run the production pipeline through API-Football and empirical derivation,
  3. settle each question from API-Football final score + statistics,
  4. score the bot's probability with the Brier score (p - outcome)^2.

The paid Odds API and web-grounded LLM pricing are disabled: backtests must not
spend paid credits or leak settled results through web search. API-Football
purges pre-match odds a few days after kickoff, so older fixtures price fewer
markets.

Usage:  python validate.py [--days 7] [--max-fixtures N]
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone

from bot.apifootball import APIFootball
from bot.pipeline import run_match


# --- question templates + settlement (kind -> (question text, settler)) ---
def build_questions(home: str, away: str) -> list[dict]:
    """Returns [{id, question, settle(ctx)->0/1 or None}] for one match."""
    T = [
        ("away_offsides_2plus",
         f"Will {away} be caught offside 2 or more times?",
         lambda c: 1 if c["stat"][away].get("Offsides", 0) >= 2 else 0),
        ("h2_more_goals",
         "Will the second half have more goals than the first half?",
         lambda c: 1 if c["h2_goals"] > c["h1_goals"] else 0),
        ("away_more_corners",
         f"Will {away} finish with more corner kicks than {home}?",
         lambda c: 1 if c["stat"][away].get("Corner Kicks", 0)
         > c["stat"][home].get("Corner Kicks", 0) else 0),
        ("home_win",
         f"Will {home} win the match?",
         lambda c: 1 if c["ft_home"] > c["ft_away"] else 0),
        ("total_goals_2_or_fewer",
         "Will the match have 2 or fewer total goals?",
         lambda c: 1 if (c["ft_home"] + c["ft_away"]) <= 2 else 0),
        ("cards_4plus",
         "Will there be 4 or more total cards shown?",
         lambda c: 1 if c["total_cards"] >= 4 else 0),
        ("home_sot_6plus",
         f"Will {home} have 6 or more shots on target?",
         lambda c: 1 if c["stat"][home].get("Shots on Goal", 0) >= 6 else 0),
        ("home_score_h2",
         f"Will {home} score in the second half?",
         lambda c: 1 if (c["ft_home"] - c["ht_home"]) >= 1 else 0),
    ]
    return [{"id": kind, "question": q, "settle": fn} for kind, q, fn in T]


def settle_context(af: APIFootball, fixture: dict) -> dict | None:
    """Pull score + per-team statistics needed to settle the questions."""
    fid = fixture["fixture"]["id"]
    home = fixture["teams"]["home"]["name"]
    away = fixture["teams"]["away"]["name"]
    score = fixture["score"]
    ht, fts = score["halftime"], score["fulltime"]
    if fts["home"] is None or ht["home"] is None:
        return None

    stat = {}
    for t in af.settled_statistics(fid):
        stat[t["team"]["name"]] = {s["type"]: (s["value"] or 0) for s in t["statistics"]}
    if home not in stat or away not in stat:
        return None

    total_cards = sum(
        stat[t].get("Yellow Cards", 0) + stat[t].get("Red Cards", 0)
        for t in (home, away)
    )
    h1 = ht["home"] + ht["away"]
    return {
        "stat": stat, "ft_home": fts["home"], "ft_away": fts["away"],
        "ht_home": ht["home"], "ht_away": ht["away"],
        "h1_goals": h1, "h2_goals": (fts["home"] + fts["away"]) - h1,
        "total_cards": total_cards,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--max-fixtures", type=int, default=None)
    args = ap.parse_args()

    af = APIFootball()
    cutoff = datetime.now(timezone.utc) - timedelta(days=args.days)
    fixtures = [
        fx for fx in af.fixtures()
        if fx["fixture"]["status"]["short"] == "FT"
        and datetime.fromisoformat(fx["fixture"]["date"]) >= cutoff
    ]
    fixtures.sort(key=lambda f: f["fixture"]["date"])
    if args.max_fixtures:
        fixtures = fixtures[-args.max_fixtures:]

    print(f"Validating {len(fixtures)} settled fixtures from the last {args.days} days\n")

    scored: list[float] = []          # bot brier scores
    coin: list[float] = []            # 50% baseline brier on same questions
    n_priced = n_skipped = 0
    skip_reasons: dict[str, int] = {}

    for fx in fixtures:
        home = fx["teams"]["home"]["name"]
        away = fx["teams"]["away"]["name"]
        date = fx["fixture"]["date"][:10]
        ctx = settle_context(af, fx)
        if not ctx:
            print(f"[{date}] {home} vs {away}: no statistics, skipped")
            continue

        questions = build_questions(home, away)
        bookmakers = af.odds(fx["fixture"]["id"])
        has_odds = bool(bookmakers)
        result = run_match(
            {"name": f"{home} vs {away}", "opening_time": fx["fixture"]["date"]},
            questions,
            af,
            llm_pricing_enabled=False,
        )
        predictions = {p.market_id: p for p in result.predictions}
        skip_by_question = dict(result.skipped)

        line = f"[{date}] {home} {ctx['ft_home']}-{ctx['ft_away']} {away}"
        print(line + ("" if has_odds else "  (odds purged)"))

        for q in questions:
            outcome = q["settle"](ctx)
            prediction = predictions.get(q["id"])
            if not prediction:
                n_skipped += 1
                reason = skip_by_question.get(q["question"], "not priced")
                skip_reasons[reason] = skip_reasons.get(reason, 0) + 1
                continue
            p = prediction.probability
            brier = (p - outcome) ** 2
            scored.append(brier)
            coin.append((0.5 - outcome) ** 2)
            n_priced += 1
            mark = "OK " if (p > 0.5) == (outcome == 1) else "miss"
            print(f"    {mark} p={p:5.2f} o={outcome}  brier={brier:.3f}  {q['question'][:48]}")

    print("\n" + "=" * 60)
    print(f"Fixtures:           {len(fixtures)}")
    print(f"Predictions priced: {n_priced}")
    print(f"Skipped:            {n_skipped}  {skip_reasons}")
    if scored:
        print(f"Bot mean Brier:     {sum(scored)/len(scored):.4f}  (lower is better)")
        print(f"Coin-flip Brier:    {sum(coin)/len(coin):.4f}  (always 50%)")
        hits = sum(1 for b in scored if b < 0.25)
        print(f"Directional acc.:   {hits}/{len(scored)} = {hits/len(scored):.0%}")


if __name__ == "__main__":
    main()
