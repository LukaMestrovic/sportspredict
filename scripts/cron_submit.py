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

At T-30 the lineups are out and there is ~1800s of headroom, so the auditable
LLM pricing layer receives the match evidence JSON, researches online odds and
context, and returns the submitted probabilities plus a per-market audit trail.

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

from bot import lineups as lineup_fetcher, llm_pricing, simulator_benchmark
from bot import submission_state
from bot.apifootball import APIFootball
from bot.config import ROOT
from bot.oddsapi import OddsAPI
from bot.pipeline import run_match, submit_with_ledger
from bot.sportspredict import SportPredict

# Submit at these many minutes-before-kickoff (largest first). A window fires on
# the first tick at or under its threshold, then is marked done for that match.
# Single window: at T-30 the XI is out and the LLM pricing layer has ample
# headroom; a later re-submit would only add market drift already researched here.
WINDOWS = (30,)
# Ignore matches further out than this so most ticks exit cheaply.
LOOKAHEAD_MIN = WINDOWS[0] + 1

STATE_DIR = submission_state.STATE_DIR
LOCK_PATH = submission_state.LOCK_PATH


def _parse_kickoff(opening_time: str) -> datetime:
    """SportPredict 'opening_time' (e.g. 2026-06-22T17:00:00.000Z) -> aware UTC."""
    dt = datetime.fromisoformat(opening_time.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _log(msg: str) -> None:
    print(f"{datetime.now(timezone.utc).isoformat(timespec='seconds')}  {msg}",
          flush=True)


def _marker(match_id: str, kickoff: datetime, window: int) -> Path:
    return submission_state.marker_path(
        match_id, kickoff, window, state_dir=STATE_DIR,
    )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="price the next match but never submit or write markers")
    ap.add_argument("--status", action="store_true",
                    help="print the next match and minutes-to-kickoff, then exit")
    ap.add_argument("--settle", action="store_true",
                    help="settle outcomes and refresh WC2026 simulator evidence")
    args = ap.parse_args()

    STATE_DIR.mkdir(parents=True, exist_ok=True)

    # Single-instance lock: if a previous tick's fire is still running, skip.
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(LOCK_PATH, "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            _log("skip: previous run still in progress")
            return
        if args.settle:
            from scripts.settle_ledger import settle_open

            stats, benchmark = settle_open()
            _log(
                f"SETTLE updated={stats['settled_predictions']} "
                f"remaining={stats['remaining_predictions']} "
                f"benchmark_observations="
                f"{benchmark['comparable_simulator_observations']} "
                f"benchmark_matches={benchmark['replayed_matches']}"
            )
        else:
            _dispatch(args)


def _dispatch(args) -> None:
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

    # The soonest match drives --status and the "too far" early exit, but a single
    # kickoff slot can hold several matches (e.g. two 22:00 games). We must process
    # EVERY due match each tick — keying only on upcoming[0] would starve all but
    # one match per slot (the others never become upcoming[0] before they drop out
    # of the future-only list at kickoff).
    next_match, next_kickoff = upcoming[0]
    next_mins = (next_kickoff - now).total_seconds() / 60.0
    next_head = next_match.get("name", next_match["id"])

    if args.status:
        slot = [(m, k) for m, k in upcoming if k == next_kickoff]
        names = ", ".join(m.get("name", m["id"]) for m, _k in slot)
        submitted = {
            m.get("name", m["id"]): [
                w for w in WINDOWS
                if submission_state.marker_exists(
                    m["id"], k, w, state_dir=STATE_DIR,
                )
            ]
            or _non_blocking_submission_status(m, k, lobby["id"])
            for m, k in slot
        }
        _log(f"next slot: {names}  kickoff {next_kickoff.isoformat()}  "
             f"in {next_mins:.1f} min  already-submitted={submitted}")
        return

    due = [(m, k) for m, k in upcoming
           if (k - now).total_seconds() / 60.0 <= LOOKAHEAD_MIN]
    if not due:
        _log(f"next: {next_head} in {next_mins:.1f} min — too far, nothing to do")
        return

    for sp_match, kickoff in due:
        _process_match(sp_match, kickoff, now, sp, event, lobby, args)


def _process_match(
    sp_match, kickoff, now, sp, event, lobby, args
) -> None:
    """Price -> submit -> mark one due match. Each match owns its own
    per-window markers, so simultaneous kickoffs are handled independently."""
    mins = (kickoff - now).total_seconds() / 60.0
    head = sp_match.get("name", sp_match["id"])

    # Pick the tightest un-fired window we've reached. Marking every window at or
    # above the one we fire collapses a missed earlier mark into a single submit.
    window = next((w for w in sorted(WINDOWS) if mins <= w
                   and not submission_state.marker_exists(
                       sp_match["id"], kickoff, w, state_dir=STATE_DIR,
                   )), None)
    if window is None:
        _log(f"{head} in {mins:.1f} min — window already submitted")
        return
    if submission_state.submitted_run_exists(
        sp_match["id"], kickoff=sp_match["opening_time"], lobby_id=lobby["id"],
    ):
        _log(f"{head} in {mins:.1f} min — submitted ledger run exists; skip")
        return

    _log(f"FIRING {window}-min window for {head} (kickoff in {mins:.1f} min)")
    # Refresh exhaustive frozen-simulator WC2026 comparisons immediately before
    # the evidence handoff. Newly settled fixtures are replayed once and cached.
    af = APIFootball(refresh_odds=True)
    try:
        simulator_benchmark.refresh(af)
    except Exception as exc:
        _log(f"  WC2026 simulator benchmark refresh warning: {exc}")
    # Each scheduled window must observe the market again. Provider instances
    # still deduplicate lookups within this run, but bypass older disk entries.
    oa = OddsAPI(refresh_odds=True)
    markets = sp.markets(lobby["id"], sp_match["id"])
    lineups = None
    try:
        fixture = af.find_fixture(sp_match["opening_time"], sp_match.get("name"))
        if fixture:
            lineups = lineup_fetcher.fetch_lineups(af, fixture, refresh=True)
    except Exception as exc:
        _log(f"  lineup fetch warning: {exc}")
    result = run_match(
        sp_match, markets, af, oa,
        llm_pricing_enabled=True, llm_pricing_refresh=True,
        lineups=lineups, minutes_before=mins,
    )

    by_src: dict[str, int] = {}
    for p in result.predictions:
        by_src[p.source] = by_src.get(p.source, 0) + 1
    summary = (f"{head}: {len(result.predictions)} priced, "
               f"{len(result.skipped)} skipped, by-source={by_src}")

    if args.dry_run:
        _log(f"DRY-RUN {summary} — not submitted")
        return

    outcome, run_ids = submit_with_ledger(
        sp, event["id"], lobby["id"], [result],
        window_min=window, minutes_before=mins,
    )
    run_id = run_ids[0]
    lineups_available = _result_has_lineups(result)

    # Mark this window and every wider one so a delayed start can't re-fire them.
    for w in WINDOWS:
        if w >= window:
            submission_state.write_marker(
                sp_match["id"], kickoff, w,
                source="cron",
                metadata={
                    "ledger_run_id": run_id,
                    "evidence_hash": getattr(result, "evidence_hash", None),
                    "evidence_path": getattr(result, "evidence_path", None),
                    "lineups_available": lineups_available,
                },
                state_dir=STATE_DIR,
            )
    _write_audit(head, kickoff, window, mins, outcome, by_src, result, run_id)
    _write_llm_pricing_report(head, kickoff, mins, result, run_id)
    landed = outcome["submitted"] + outcome["updated"] + outcome["unchanged"]
    _log(f"UPSERT {head}: created={outcome['submitted']} updated={outcome['updated']} "
         f"unchanged={outcome['unchanged']} failed={outcome['failed']} "
         f"(landed={landed}/{len(outcome['payload'])}) — {summary}")
    if outcome["failed"]:
        _log(f"  WARN {outcome['failed']} rejected: {outcome['errors'][:2]}")


def _result_has_lineups(result) -> bool:
    evidence_json = getattr(result, "evidence_json", None) or {}
    return submission_state.evidence_has_lineups(
        getattr(result, "evidence_path", None)
    ) or submission_state.evidence_lineups_available(
        (evidence_json.get("match") or {}).get("lineups")
    )


def _non_blocking_submission_status(match: dict, kickoff: datetime, lobby_id: str):
    if submission_state.submitted_run_with_lineups_exists(
        match["id"], kickoff=match["opening_time"], lobby_id=lobby_id,
    ):
        return "ledger-with-lineups"
    if submission_state.submitted_run_exists(
        match["id"], kickoff=match["opening_time"], lobby_id=lobby_id,
    ):
        return "ledger-submitted"
    if any(_marker(match["id"], kickoff, w).exists() for w in WINDOWS):
        return "marker-submitted"
    return "none"


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
        "evidence_path": getattr(result, "evidence_path", None),
        "evidence_hash": getattr(result, "evidence_hash", None),
        "lineups_available": _result_has_lineups(result),
        "llm_match_read_path": getattr(result, "llm_match_read_path", None),
        "llm_pricing_audit_path": getattr(result, "llm_pricing_audit_path", None),
        "llm_pricing_report_path": getattr(result, "llm_pricing_report_path", None),
        "llm_pricing_briefing": getattr(result, "llm_pricing_briefing", None),
        "llm_pricing_sources": getattr(result, "llm_pricing_sources", []),
        "predictions": [
            {"question": p.question,
             "probability": p.probability_int,
             "rationale": p.llm_reasoning_summary,
             "audit": p.llm_audit,
             "source": p.source, "market_id": p.market_id}
            for p in result.predictions
        ],
    }
    path = ROOT / "logs" / "cron_submissions.jsonl"
    path.parent.mkdir(exist_ok=True)
    with open(path, "a") as f:
        f.write(json.dumps(rec) + "\n")


def _write_llm_pricing_report(head, kickoff, mins, result, run_id) -> None:
    """Human-readable pointer summary for the auditable LLM pricing run."""
    lines = [
        f"=== {head} ===",
        f"kickoff {kickoff.isoformat()}  (T-{mins:.0f} min)  model={llm_pricing.MODEL}  "
        f"ledger_run={run_id}",
    ]
    if getattr(result, "evidence_path", None):
        lines.append(f"[evidence] {result.evidence_path}")
    if getattr(result, "llm_match_read_path", None):
        lines.append(f"[match read] {result.llm_match_read_path}")
    if getattr(result, "llm_pricing_report_path", None):
        lines.append(f"[full audit] {result.llm_pricing_report_path}")
    if getattr(result, "llm_pricing_briefing", None):
        lines.append(f"\n[match-read + briefing] {result.llm_pricing_briefing}")
    if getattr(result, "llm_pricing_sources", None):
        lines.append("[sources] " + ", ".join(result.llm_pricing_sources[:10]))
    lines.append("")
    lines.append(f"{'prob':>5} {'src':>12} {'n':>3}  question")
    for p in result.predictions:
        lines.append(
            f"{p.probability_int:>4}% {(p.source or '?')[:12]:>12} "
            f"{p.n_books or 0:>3}  {p.question}"
        )
        if p.llm_reasoning_summary:
            lines.append(f"{'':>8}↳ {p.llm_reasoning_summary}")
    lines.append(f"\n{len(result.predictions)} priced, {len(result.skipped)} skipped, "
                 f"audit={getattr(result, 'llm_pricing_report_path', None)}")
    lines.append(f"USAGE: {json.dumps(llm_pricing.LAST_USAGE)}")

    outdir = ROOT / "logs" / "llm_pricing_runs"
    outdir.mkdir(parents=True, exist_ok=True)
    slug = head.replace(" ", "_").replace("/", "_")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    (outdir / f"{slug}_{stamp}.md").write_text("\n".join(lines) + "\n")


if __name__ == "__main__":
    main()
