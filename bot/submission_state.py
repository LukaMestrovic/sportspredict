"""Shared submission markers for cron and manual runs."""
from __future__ import annotations

import json
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from . import config, ledger


STATE_DIR = config.ROOT / "cache" / "cron_state"
LOCK_PATH = config.ROOT / "cache" / "cron_submit.lock"


def marker_path(
    match_id: str,
    kickoff: datetime,
    window: int,
    *,
    state_dir: Path = STATE_DIR,
) -> Path:
    epoch = int(kickoff.timestamp())
    return state_dir / f"{match_id}_{epoch}__w{window}.done"


def marker_exists(
    match_id: str,
    kickoff: datetime,
    window: int,
    *,
    state_dir: Path = STATE_DIR,
) -> bool:
    return marker_path(match_id, kickoff, window, state_dir=state_dir).exists()


def write_marker(
    match_id: str,
    kickoff: datetime,
    window: int,
    *,
    source: str,
    metadata: dict | None = None,
    state_dir: Path = STATE_DIR,
) -> Path:
    path = marker_path(match_id, kickoff, window, state_dir=state_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "match_id": match_id,
        "kickoff": kickoff.isoformat(),
        "window_min": window,
        "source": source,
        "marked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    if metadata:
        payload.update(metadata)
    path.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
    return path


def submitted_run_exists(
    match_id: str,
    *,
    kickoff: str | None = None,
    lobby_id: str | None = None,
    path: Path = ledger.LEDGER_PATH,
) -> bool:
    if not path.exists():
        return False
    clauses = ["match_id = ?", "status = 'submitted'"]
    params: list[str] = [match_id]
    if kickoff is not None:
        clauses.append("kickoff = ?")
        params.append(kickoff)
    if lobby_id is not None:
        clauses.append("lobby_id = ?")
        params.append(lobby_id)
    query = f"SELECT 1 FROM runs WHERE {' AND '.join(clauses)} LIMIT 1"
    with closing(ledger.connect(path)) as db:
        row = db.execute(query, params).fetchone()
    return row is not None
