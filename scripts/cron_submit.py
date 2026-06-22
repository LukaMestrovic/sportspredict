#!/usr/bin/env python3
"""Autonomous submitter: cron runs this every minute; it submits the next
match's predictions at the 30-minute and 5-minute marks before kickoff.

Design
------
Cron can't fire "30 min before a variable kickoff", so this is a *dispatcher*:
run it once a minute and let it decide. Each tick it finds the soonest open
match and submits once per window (30, then 5) using marker files so it never
re-submits on the intervening ticks. A file lock prevents overlapping ticks
(a fire calls the LLM/odds and can outlast one minute) from double-submitting.

Determinism is preserved: recurring templates are local, LLM fallbacks are
cached, and the web layer stays off (EXTERNAL_FALLBACK=0). Each window forces
one fresh provider observation so the 5-minute run sees late movement and deep
markets that were unavailable at 30 minutes.

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

from bot.apifootball import APIFootball
from bot.config import ROOT
from bot.oddsapi import OddsAPI
from bot.pipeline import run_match, submit_predictions
from bot.sportspredict import SportPredict

# Submit at these many minutes-before-kickoff (largest first). A window fires on
# the first tick at or under its threshold, then is marked done for that match.
WINDOWS = (30, 5)
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

    by_src: dict[str, int] = {}
    for p in result.predictions:
        by_src[p.source] = by_src.get(p.source, 0) + 1
    summary = (f"{head}: {len(result.predictions)} priced, "
               f"{len(result.skipped)} skipped, by-source={by_src}")

    if args.dry_run:
        _log(f"DRY-RUN {summary} — not submitted")
        return

    batch = submit_predictions(sp, lobby["id"], [result])

    # Mark this window and every wider one so a delayed start can't re-fire them.
    for w in WINDOWS:
        if w >= window:
            _marker(sp_match["id"], kickoff, w).touch()
    _write_audit(head, kickoff, window, mins, batch, by_src, result)
    _log(f"SUBMITTED {len(batch)} predictions — {summary}")


def _write_audit(head, kickoff, window, mins, batch, by_src, result) -> None:
    """One JSON line per fire, for an auditable submission history."""
    rec = {
        "submitted_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "match": head,
        "kickoff": kickoff.isoformat(),
        "window_min": window,
        "minutes_before": round(mins, 1),
        "n_submitted": len(batch),
        "by_source": by_src,
        "predictions": [
            {"question": p.question, "probability": p.probability_int,
             "source": p.source, "market_id": p.market_id}
            for p in result.predictions
        ],
    }
    path = ROOT / "logs" / "cron_submissions.jsonl"
    path.parent.mkdir(exist_ok=True)
    with open(path, "a") as f:
        f.write(json.dumps(rec) + "\n")


if __name__ == "__main__":
    main()
