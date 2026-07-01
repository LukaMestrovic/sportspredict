"""Offline validation of the goal-minute timeline against real goal minutes.

The prompt assumed timing markets have no offline labels. They do: ``sphybrid ingest`` already cached
the API-Football *events* endpoint (for penalties), and those payloads carry every goal's minute
(``time.elapsed``) and scoring team. So for the 4,173 API-Football fixtures in the stat table we can
build true labels for first-goal and early-goal markets and Brier-score the timeline — no new calls.

For each fixture we read the cached events, attribute each goal to home/away (own goals flipped),
take the earliest as the opening goal, simulate the fixture, and compare. We report the timeline
Brier against two reference points the deployed bot otherwise has nothing to beat: a constant
base-rate predictor and an Elo goal-share heuristic.
We also report calibration (reliability deciles + ECE), the property that actually matters for a
probability that is submitted as an integer percent.

This reads the gitignored ``data/raw/apifootball`` cache, so it is a local validation tool (like the
event cache itself), not part of CI. The pure event-parsing function is unit-tested on synthetic JSON.
"""

from __future__ import annotations

import argparse
import json

import numpy as np
import pandas as pd

from sportspredict.config import Settings, default_settings
from sportspredict.features.context import MatchContext
from sportspredict.ingest.elo import load_elo_table
from sportspredict.model import simulate

from ..rates import make_rate_model
from ..rates.ingest_apifootball import Canonicalizer, _cache_path
from .markets import FIRST_HYDRATION_MINUTE, SECOND_HYDRATION_MINUTE
from .timeline import GoalTimeline, card_timeline
from .timing import TimingModel

_GOAL_DETAILS = {"Normal Goal", "Penalty"}  # "Own Goal" is handled by flipping sides; "Missed Penalty" ignored


def parse_goal_events(
    events: list[dict], canon: Canonicalizer, home: str, away: str
) -> list[tuple[float, str]]:
    """Return ``(minute, side)`` for each goal, side in {"home","away"}; own goals credited to the foe."""
    goals: list[tuple[float, str]] = []
    for e in events or []:
        if e.get("type") != "Goal":
            continue
        detail = str(e.get("detail") or "")
        if detail not in _GOAL_DETAILS and detail != "Own Goal":
            continue
        name = canon((e.get("team") or {}).get("name") or "")
        if name == home:
            scorer = "away" if detail == "Own Goal" else "home"
        elif name == away:
            scorer = "home" if detail == "Own Goal" else "away"
        else:
            continue  # a team we cannot map (rare) — skip this goal
        t = e.get("time") or {}
        if float(t.get("elapsed") or 0) > 90:
            continue  # first-goal contracts are regulation-only; 90+added time remains included
        minute = float(t.get("elapsed") or 0) + float(t.get("extra") or 0)
        goals.append((minute, scorer))
    goals.sort(key=lambda g: g[0])
    return goals


def _events_for(match_id) -> list[dict]:
    path = _cache_path("fixtures/events", {"fixture": int(match_id)})
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text()).get("response", [])
    except Exception:
        return []


def _labels(table: pd.DataFrame, canon: Canonicalizer) -> pd.DataFrame:
    """Attach true first-goal side and early-goal flags from the cached events to each AF row."""
    recs = []
    for row in table.to_dict("records"):
        events = _events_for(row["match_id"])
        is_goal = lambda e: (
            e.get("type") == "Goal"
            and str(e.get("detail") or "") in (_GOAL_DETAILS | {"Own Goal"})
        )
        goals = parse_goal_events(events, canon,
                                  str(row["home_team"]), str(row["away_team"]))
        all_goal_times = [
            int((e.get("time") or {}).get("elapsed") or 0)
            + int((e.get("time") or {}).get("extra") or 0)
            for e in events if is_goal(e)
        ]
        if not goals:
            first = "none"
        else:
            first = goals[0][1]
        recs.append({
            "match_id": row["match_id"], "home_team": row["home_team"], "away_team": row["away_team"],
            "home_elo": float(row.get("home_elo", 1500.0)), "away_elo": float(row.get("away_elo", 1500.0)),
            "stage": str(row.get("stage", "group")),
            "first_home": int(first == "home"), "first_away": int(first == "away"),
            "any_goal": int(bool(goals)),
            "early15": int(any(m <= 15 for m, _ in goals)),
            "goal_before_hydration": int(any(m <= FIRST_HYDRATION_MINUTE for m, _ in goals)),
            "goal_after_second_hydration": int(
                any(m > SECOND_HYDRATION_MINUTE for m in all_goal_times)
            ),
            "goal_h1_stoppage": int(any(
                is_goal(e) and int((e.get("time") or {}).get("elapsed") or 0) == 45
                and int((e.get("time") or {}).get("extra") or 0) > 0
                for e in events
            )),
            "goal_h2_stoppage": int(any(
                is_goal(e) and int((e.get("time") or {}).get("elapsed") or 0) == 90
                and int((e.get("time") or {}).get("extra") or 0) > 0
                for e in events
            )),
            "card_after_second_hydration": int(any(
                e.get("type") == "Card"
                and (int((e.get("time") or {}).get("elapsed") or 0)
                     + int((e.get("time") or {}).get("extra") or 0)) > SECOND_HYDRATION_MINUTE
                for e in events
            )),
            "substitution_before_halftime": int(any(
                e.get("type") == "subst"
                and int((e.get("time") or {}).get("elapsed") or 0) <= 45
                for e in events
            )),
        })
    return pd.DataFrame.from_records(recs)


def _brier(p: np.ndarray, y: np.ndarray) -> float:
    return float(np.mean((p - y) ** 2))


def _ece(p: np.ndarray, y: np.ndarray, bins: int = 10) -> float:
    """Expected calibration error over equal-width probability bins."""
    edges = np.linspace(0.0, 1.0, bins + 1)
    idx = np.clip(np.digitize(p, edges[1:-1]), 0, bins - 1)
    err = 0.0
    for b in range(bins):
        m = idx == b
        if m.any():
            err += m.mean() * abs(p[m].mean() - y[m].mean())
    return float(err)


def evaluate(settings: Settings, *, elo_csv: str, n_sims: int, limit: int | None,
             held: list[str] | None) -> dict:
    """Brier + calibration of the timeline's first-goal / early-goal predictions vs real minutes."""
    table = pd.read_parquet(settings.path("data/processed/history_stat_table.parquet"))
    if "source" in table:
        table = table[table["source"] == "apifootball"].copy()
    else:
        # Expanded tables may omit the source column; cached event presence is the authoritative
        # local membership test and avoids scoring StatsBomb-only rows as no-event matches.
        table = table[table["match_id"].map(lambda mid: _cache_path(
            "fixtures/events", {"fixture": int(mid)}).exists())].copy()
    if held:
        table = table[table["tournament"].isin(set(held))]
    if limit:
        table = table.head(limit)
    elo_table = load_elo_table(settings.path(elo_csv))
    canon = Canonicalizer(elo_table)
    labels = _labels(table, canon)

    model = make_rate_model(settings)
    seed = int(settings.seed) if settings.seed is not None else 0
    event_path = settings.raw.get("postsim", {}).get("event_model")
    timing = TimingModel.load(settings.path(event_path) if event_path else None)
    ph, pa, pe = [], [], []
    p_hydration, p_late, p_h1_stop, p_h2_stop, p_late_card, p_early_sub = [], [], [], [], [], []
    for r in labels.to_dict("records"):
        ctx = MatchContext(r["home_team"], r["away_team"], elo_a=r["home_elo"], elo_b=r["away_elo"],
                           stage=r["stage"])
        rates = model.build(ctx)
        out = simulate(rates, n_sims=n_sims, rng=np.random.default_rng(seed), settings=settings)
        tl = GoalTimeline.from_outcome(out, np.random.default_rng(np.random.SeedSequence([seed, 0x71_3E])),
                                       et_minutes=float(settings.raw.get("postsim", {}).get("et_minutes", 30.0)),
                                       timing=timing)
        cards = card_timeline(
            out, timing, np.random.default_rng(np.random.SeedSequence([seed, 0xCA_4D]))
        )
        ph.append(float(tl.first_scorer_is(0).mean()))
        pa.append(float(tl.first_scorer_is(1).mean()))
        pe.append(float(tl.any_goal_in_window(0, 15).mean()))
        p_hydration.append(float(
            tl.any(tl.select(through=FIRST_HYDRATION_MINUTE, phases={"1H"})).mean()
        ))
        p_late.append(float(
            tl.any(tl.select(after=SECOND_HYDRATION_MINUTE, phases={"2H", "ET"})).mean()
        ))
        p_h1_stop.append(float(tl.any(tl.select(stoppage="1H")).mean()))
        p_h2_stop.append(float(tl.any(tl.select(stoppage="2H")).mean()))
        p_late_card.append(float(
            cards.any(cards.select(after=SECOND_HYDRATION_MINUTE, phases={"2H", "ET"})).mean()
        ))
        p_early_sub.append(timing.rate("substitution_before_halftime", r["stage"], 0.10))
    ph, pa, pe = np.array(ph), np.array(pa), np.array(pe)
    yh = labels["first_home"].to_numpy(float)
    ya = labels["first_away"].to_numpy(float)
    ye = labels["early15"].to_numpy(float)

    # References the deployed bot could otherwise fall back to (it cannot answer these at all today):
    base_home = float(yh.mean())                                   # constant empirical base rate
    share = 1.0 / (1.0 + 10.0 ** (-(labels["home_elo"] - labels["away_elo"]) / 400.0))  # Elo win-prob proxy
    p_any = float(labels["any_goal"].mean())
    share_home = (share.to_numpy() * p_any)                        # Elo goal-share heuristic for "home first"

    def scored(pred, label):
        p = np.asarray(pred, dtype=float)
        y = labels[label].to_numpy(float)
        return {
            "timeline_brier": _brier(p, y),
            "baserate_brier": _brier(np.full_like(y, float(y.mean())), y),
            "timeline_ece": _ece(p, y),
            "pred_mean": float(p.mean()), "label_mean": float(y.mean()),
        }

    return {
        "n": int(len(labels)),
        "first_goal_home": {
            "timeline_brier": _brier(ph, yh),
            "baserate_brier": _brier(np.full_like(yh, base_home), yh),
            "elo_share_brier": _brier(share_home, yh),
            "timeline_ece": _ece(ph, yh),
            "pred_mean": float(ph.mean()), "label_mean": float(yh.mean()),
        },
        "first_goal_away": {
            "timeline_brier": _brier(pa, ya),
            "baserate_brier": _brier(np.full_like(ya, float(ya.mean())), ya),
            "timeline_ece": _ece(pa, ya),
            "pred_mean": float(pa.mean()), "label_mean": float(ya.mean()),
        },
        "early_goal_0_15": {
            "timeline_brier": _brier(pe, ye),
            "baserate_brier": _brier(np.full_like(ye, float(ye.mean())), ye),
            "timeline_ece": _ece(pe, ye),
            "pred_mean": float(pe.mean()), "label_mean": float(ye.mean()),
        },
        "goal_before_first_hydration": scored(p_hydration, "goal_before_hydration"),
        "goal_after_second_hydration": scored(p_late, "goal_after_second_hydration"),
        "goal_h1_stoppage": scored(p_h1_stop, "goal_h1_stoppage"),
        "goal_h2_stoppage": scored(p_h2_stop, "goal_h2_stoppage"),
        "card_after_second_hydration": scored(p_late_card, "card_after_second_hydration"),
        "substitution_before_halftime": scored(p_early_sub, "substitution_before_halftime"),
    }


def _print(out: dict) -> None:
    print(f"=== TIMELINE VALIDATION (events-derived labels)  n={out['n']} fixtures ===")
    for fam, d in out.items():
        if fam == "n":
            continue
        print(f"\n{fam}")
        print(f"  timeline Brier  {d['timeline_brier']:.5f}   (pred mean {d['pred_mean']:.3f} vs "
              f"label {d['label_mean']:.3f}, ECE {d['timeline_ece']:.4f})")
        print(f"  base-rate Brier {d['baserate_brier']:.5f}")
        if "elo_share_brier" in d:
            print(f"  Elo-share Brier {d['elo_share_brier']:.5f}")
        better = "better" if d["timeline_brier"] < d["baserate_brier"] else "worse"
        print(f"  -> timeline is {better} than the base rate by "
              f"{d['baserate_brier'] - d['timeline_brier']:+.5f}")


def _main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Validate the goal-minute timeline against cached real goal minutes.")
    ap.add_argument("--elo-csv", default="data/raw/elo.csv")
    ap.add_argument("--n-sims", dest="n_sims", type=int, default=4000)
    ap.add_argument("--limit", type=int, default=None, help="cap fixtures (for a quick run)")
    ap.add_argument("--held-tournaments", dest="held", default=None,
                    help="comma-separated tournaments to score (default: all API-Football rows)")
    ap.add_argument("--json", dest="as_json", action="store_true")
    args = ap.parse_args(argv)
    settings = default_settings()
    held = [t.strip() for t in args.held.split(",")] if args.held else None
    out = evaluate(settings, elo_csv=args.elo_csv, n_sims=args.n_sims, limit=args.limit, held=held)
    print(json.dumps(out, indent=2)) if args.as_json else _print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
