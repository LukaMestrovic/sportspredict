#!/usr/bin/env python3
"""Autonomous submitter: cron runs this every minute; it submits the next
match's predictions at the 30-minute mark before kickoff.

Design
------
Cron can't fire "30 min before a variable kickoff", so this is a *dispatcher*:
run it once a minute and let it decide. Each tick it finds the soonest open
match and submits once at the 30-minute window using a marker file so it never
re-submits on the intervening ticks. A file lock prevents overlapping ticks
(a fire calls the LLM/odds and can outlast one minute) from double-submitting.

At T-30 the lineups are out and there is ~1800s of headroom, so the calibration
layer (bot/calibrate.py, gated by CALIBRATE_ENABLED) fires its single per-match
LLM call here, between pricing and submission, tilting the anchors in place.

Determinism is preserved for the anchors: recurring templates are local, LLM
fallbacks are cached, and the web layer stays off (EXTERNAL_FALLBACK=0). The
window forces one fresh provider observation so the run sees the latest market.

Manual checks:
  python -m scripts.cron_submit --dry-run   # decide + price, never submit/mark
  python -m scripts.cron_submit --status    # just print the next match + ETA
"""
from __future__ import annotations

import argparse
import fcntl
import json
from datetime import datetime, timezone
from pathlib import Path

from bot import calibrate
from bot.apifootball import APIFootball
from bot.config import ROOT
from bot.oddsapi import OddsAPI
from bot.pipeline import run_match, submit_with_ledger
from bot.sportspredict import SportPredict

# Submit at these many minutes-before-kickoff (largest first). A window fires on
# the first tick at or under its threshold, then is marked done for that match.
# Single window: at T-30 the XI is out and the calibration layer has ample
# headroom; a later re-submit would only add market drift already re-priced here.
WINDOWS = (30,)
# Ignore matches further out than this so most ticks exit cheaply.
LOOKAHEAD_MIN = WINDOWS[0] + 1

STATE_DIR = ROOT / "cache" / "cron_state"
LOCK_PATH = ROOT / "cache" / "cron_submit.lock"


def _parse_kickoff(opening_time: str) -> datetime:
    """SportPredict 'opening_time' (e.g. 2026-06-22T17:00:00.000Z) -> aware UTC."""
    dt = datetime.fromisoformat(opening_time.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _log(msg: str) -> None:
    print(f"{datetime.now(timezone.utc).isoformat(timespec='seconds')}  {msg}",
          flush=True)


def _marker(match_id: str, kickoff: datetime, window: int) -> Path:
    epoch = int(kickoff.timestamp())
    return STATE_DIR / f"{match_id}_{epoch}__w{window}.done"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="price the next match but never submit or write markers")
    ap.add_argument("--status", action="store_true",
                    help="print the next match and minutes-to-kickoff, then exit")
    args = ap.parse_args()

    STATE_DIR.mkdir(parents=True, exist_ok=True)

    # Single-instance lock: if a previous tick's fire is still running, skip.
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    lock = open(LOCK_PATH, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        _log("skip: previous run still in progress")
        return

    sp = SportPredict()
    event = sp.event()
    lobby = sp.lobby(event["id"])
    matches = sp.matches(event["id"], lobby["id"])

    now = datetime.now(timezone.utc)
    upcoming = sorted(
        ((m, _parse_kickoff(m["opening_time"])) for m in matches),
        key=lambda mk: mk[1],
    )
    upcoming = [(m, k) for m, k in upcoming if k > now]
    if not upcoming:
        _log("no upcoming open matches")
        return

    sp_match, kickoff = upcoming[0]
    mins = (kickoff - now).total_seconds() / 60.0
    head = sp_match.get("name", sp_match["id"])

    if args.status:
        fired = [w for w in WINDOWS if _marker(sp_match["id"], kickoff, w).exists()]
        _log(f"next: {head}  kickoff {kickoff.isoformat()}  "
             f"in {mins:.1f} min  already-submitted={fired or 'none'}")
        return

    if mins > LOOKAHEAD_MIN:
        _log(f"next: {head} in {mins:.1f} min — too far, nothing to do")
        return

    # Pick the tightest un-fired window we've reached. Marking every window at or
    # above the one we fire collapses a missed earlier mark into a single submit.
    window = next((w for w in sorted(WINDOWS) if mins <= w
                   and not _marker(sp_match["id"], kickoff, w).exists()), None)
    if window is None:
        _log(f"next: {head} in {mins:.1f} min — windows already submitted")
        return

    _log(f"FIRING {window}-min window for {head} (kickoff in {mins:.1f} min)")
    # Each scheduled window must observe the market again. Provider instances
    # still deduplicate lookups within this run, but bypass older disk entries.
    af = APIFootball(refresh_odds=True)
    oa = OddsAPI(refresh_odds=True)
    markets = sp.markets(lobby["id"], sp_match["id"])
    result = run_match(sp_match, markets, af, oa, allow_external=False)

    # LLM calibration: one web-grounded call (cached per match), tilts anchors in
    # place. Gated by CALIBRATE_ENABLED; any error degrades to the raw anchors.
    if calibrate.ENABLED and result.fixture and result.predictions:
        try:
            fixture_id = result.fixture["fixture"]["id"]
            calibrate.calibrate(result, af.lineups(fixture_id), mins)
        except Exception as exc:  # never let calibration block a submission
            _log(f"  calibration error (using raw anchors): {exc}")

    by_src: dict[str, int] = {}
    for p in result.predictions:
        by_src[p.source] = by_src.get(p.source, 0) + 1
    tilted = sum(1 for p in result.predictions if getattr(p, "applied_delta", 0))
    summary = (f"{head}: {len(result.predictions)} priced, "
               f"{len(result.skipped)} skipped, {tilted} tilted, by-source={by_src}")

    if args.dry_run:
        _log(f"DRY-RUN {summary} — not submitted")
        return

    outcome, run_ids = submit_with_ledger(
        sp, event["id"], lobby["id"], [result],
        window_min=window, minutes_before=mins,
    )
    run_id = run_ids[0]

    # Mark this window and every wider one so a delayed start can't re-fire them.
    for w in WINDOWS:
        if w >= window:
            _marker(sp_match["id"], kickoff, w).touch()
    _write_audit(head, kickoff, window, mins, outcome, by_src, result, run_id)
    _write_calibration_report(head, kickoff, mins, result, run_id)
    landed = outcome["submitted"] + outcome["updated"] + outcome["unchanged"]
    _log(f"UPSERT {head}: created={outcome['submitted']} updated={outcome['updated']} "
         f"unchanged={outcome['unchanged']} failed={outcome['failed']} "
         f"(landed={landed}/{len(outcome['payload'])}) — {summary}")
    if outcome["failed"]:
        _log(f"  WARN {outcome['failed']} rejected: {outcome['errors'][:2]}")


def _write_audit(head, kickoff, window, mins, outcome, by_src, result, run_id) -> None:
    """One JSON line per fire, for an auditable submission history."""
    rec = {
        "submitted_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "match": head,
        "kickoff": kickoff.isoformat(),
        "window_min": window,
        "minutes_before": round(mins, 1),
        "ledger_run_id": run_id,
        "n_predictions": len(outcome["payload"]),
        "created": outcome["submitted"],
        "updated": outcome["updated"],
        "unchanged": outcome["unchanged"],
        "failed": outcome["failed"],
        "errors": outcome["errors"][:5],
        "by_source": by_src,
        "calibration_briefing": getattr(result, "calibration_briefing", None),
        "calibration_sources": getattr(result, "calibration_sources", []),
        "predictions": [
            {"question": p.question, "probability": p.probability_int,
             "anchor": p.anchor_probability_int, "tilt": p.tilt_points,
             "rationale": p.calibration_rationale,
             "source": p.source, "market_id": p.market_id}
            for p in result.predictions
        ],
    }
    path = ROOT / "logs" / "cron_submissions.jsonl"
    path.parent.mkdir(exist_ok=True)
    with open(path, "a") as f:
        f.write(json.dumps(rec) + "\n")


def _write_calibration_report(head, kickoff, mins, result, run_id) -> None:
    """Human-readable per-match calibration summary in logs/calibration_runs/.

    Mirrors the manual preview runner: the match-read briefing, the sources, and
    a per-question anchor->calibrated / tilt / cap / rationale table — so each
    cron fire leaves a reviewable record alongside the ledger and the JSONL audit.
    """
    lines = [
        f"=== {head} ===",
        f"kickoff {kickoff.isoformat()}  (T-{mins:.0f} min)  model={calibrate.MODEL}  "
        f"ledger_run={run_id}",
    ]
    if getattr(result, "calibration_briefing", None):
        lines.append(f"\n[match-read + briefing] {result.calibration_briefing}")
    if getattr(result, "calibration_sources", None):
        lines.append("[sources] " + ", ".join(result.calibration_sources[:10]))
    lines.append("")
    lines.append(f"{'anchor→cal':>11} {'tilt':>5} {'cap':>4} {'src':>6} {'n':>3}  question")
    for p in result.predictions:
        moved = p.anchor_probability_int not in (None, p.probability_int)
        move = (f"{p.anchor_probability_int}→{p.probability_int}" if moved
                else f"{p.probability_int}")
        tilt = f"{p.tilt_points:+g}" if p.tilt_points else "·"
        cap = calibrate.cap_for_books(p.n_books or 0)
        lines.append(f"{move:>11} {tilt:>5} {cap:>4} {(p.source or '?')[:6]:>6} "
                     f"{p.n_books or 0:>3}  {p.question}")
        if p.calibration_rationale:
            lines.append(f"{'':>13}↳ {p.calibration_rationale}")
    tilted = sum(1 for p in result.predictions if getattr(p, "applied_delta", 0))
    lines.append(f"\n{len(result.predictions)} priced, {len(result.skipped)} skipped, "
                 f"{tilted} tilted")
    lines.append(f"USAGE: {json.dumps(calibrate.LAST_USAGE)}")

    outdir = ROOT / "logs" / "calibration_runs"
    outdir.mkdir(parents=True, exist_ok=True)
    slug = head.replace(" ", "_").replace("/", "_")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    (outdir / f"{slug}_{stamp}.md").write_text("\n".join(lines) + "\n")


if __name__ == "__main__":
    main()
