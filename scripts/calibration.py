#!/usr/bin/env python3
"""Synchronize, inspect, and evaluate outcome calibration state."""
from __future__ import annotations

import argparse
import json
from contextlib import closing
from pathlib import Path

from bot import calibration, ledger
from bot.sportspredict import SportPredict
from bot.web import WebAPI


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ledger", type=Path, default=ledger.LEDGER_PATH)
    parser.add_argument("--lobby-id", default=None)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("sync", help="import new explicit outcomes and refit")
    sub.add_parser("status", help="show durable calibration state")
    evaluate = sub.add_parser("evaluate", help="show local prequential diagnostics")
    evaluate.add_argument("--json", action="store_true", help="print full JSON")
    args = parser.parse_args()

    if args.command == "sync":
        sp = SportPredict()
        event = sp.event()
        lobbies = sp._get("/lobbies", event_id=event["id"])
        if not lobbies:
            raise RuntimeError("no Probability Cup lobby found")
        lobby = next(
            (row for row in lobbies if args.lobby_id in (None, row["id"])), None
        )
        if lobby is None:
            raise RuntimeError(f"lobby not found: {args.lobby_id}")
        result = calibration.sync_and_refit(
            sp, WebAPI(), event, lobby, path=args.ledger,
        )
        print(json.dumps(result, indent=2, sort_keys=True))
        return

    lobby_id = args.lobby_id or _local_lobby_id(args.ledger)
    current = calibration.status(lobby_id, path=args.ledger)
    if args.command == "status":
        _print_status(current)
    elif args.json:
        print(json.dumps((current.get("snapshot") or {}).get("diagnostics"),
                         indent=2, sort_keys=True))
    else:
        _print_evaluation(current)


def _print_status(current: dict) -> None:
    state = current.get("state") or {}
    snapshot = current.get("snapshot") or {}
    gate = snapshot.get("global_gate") or {}
    print(
        f"Calibration observations: {current['observations']} across "
        f"{current['matches']} matches"
    )
    print(f"Settled sync state: {current['match_statuses']}")
    print(f"Legacy backfill complete: {bool(state.get('legacy_backfill_complete'))}")
    print(f"Last sync: {state.get('last_sync_at') or 'never'}")
    if state.get("last_error"):
        print(f"Last error: {state['last_error']}")
    print(f"Active snapshot: {snapshot.get('model_id') or 'identity (none)'}")
    print(f"Global gate: {gate.get('active', False)} — {gate.get('reason', 'no snapshot')}")


def _print_evaluation(current: dict) -> None:
    snapshot = current.get("snapshot") or {}
    diagnostics = snapshot.get("diagnostics") or {}
    gate = snapshot.get("global_gate") or {}
    print(f"Snapshot: {snapshot.get('model_id') or 'none'}")
    print(
        f"Prequential: {diagnostics.get('prequential_observations', 0)} markets / "
        f"{diagnostics.get('prequential_matches', 0)} matches"
    )
    raw = diagnostics.get("raw_mean_brier")
    calibrated = diagnostics.get("calibrated_mean_brier")
    delta = diagnostics.get("mean_brier_delta")
    if raw is not None:
        print(f"Raw mean Brier:        {raw:.6f}")
        print(f"Calibrated mean Brier: {calibrated:.6f}")
        print(f"Delta (cal - raw):     {delta:+.6f}")
    print(f"Global gate: {gate.get('active', False)} — {gate.get('reason', 'no snapshot')}")
    print("Family gates:")
    for family, row in sorted((snapshot.get("family_gates") or {}).items()):
        print(f"  {family:<30} {str(row.get('active', False)):<5} {row.get('reason')}")
    print("Cohort gates:")
    for cohort, row in sorted((snapshot.get("cohort_gates") or {}).items()):
        print(f"  {cohort:<30} {str(row.get('active', False)):<5} {row.get('reason')}")


def _local_lobby_id(path: Path) -> str:
    """Resolve local calibration state without any network request."""
    with closing(ledger.connect(path)) as db:
        rows = db.execute(
            "SELECT lobby_id FROM calibration_state ORDER BY initialized_at"
        ).fetchall()
    if len(rows) != 1:
        raise RuntimeError("pass --lobby-id when the ledger has zero or multiple lobbies")
    return rows[0]["lobby_id"]


if __name__ == "__main__":
    main()
