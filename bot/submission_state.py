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


def marker_with_lineups_exists(
    match_id: str,
    kickoff: datetime,
    window: int,
    *,
    state_dir: Path = STATE_DIR,
) -> bool:
    path = marker_path(match_id, kickoff, window, state_dir=state_dir)
    if not path.exists():
        return False
    metadata = _read_marker(path)
    if metadata.get("lineups_available") is True:
        return True
    evidence_path = metadata.get("evidence_path")
    return evidence_has_lineups(evidence_path) if evidence_path else False


def marker_blocks_cron(
    match_id: str,
    kickoff: datetime,
    window: int,
    *,
    state_dir: Path = STATE_DIR,
) -> bool:
    """Return whether an existing marker should suppress the automated run.

    Cron's own markers always block repeat cron fires. Manual markers block only
    when they are lineup-backed; this lets an older/manual no-lineups submission
    be improved by the T-30 automated run once confirmed XIs exist.
    """
    path = marker_path(match_id, kickoff, window, state_dir=state_dir)
    if not path.exists():
        return False
    metadata = _read_marker(path)
    if metadata.get("source") in {"manual-chatgpt", "manual-chatgpt-started"}:
        return marker_with_lineups_exists(
            match_id, kickoff, window, state_dir=state_dir,
        )
    return True


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


def submitted_run_with_lineups_exists(
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
    query = (
        f"SELECT evidence_path FROM runs WHERE {' AND '.join(clauses)} "
        "ORDER BY submitted_at DESC, recorded_at DESC"
    )
    with closing(ledger.connect(path)) as db:
        rows = db.execute(query, params).fetchall()
    return any(evidence_has_lineups(row["evidence_path"]) for row in rows)


def evidence_has_lineups(pathish) -> bool:
    if not pathish:
        return False
    path = _resolve_path(Path(str(pathish)))
    if not path.exists():
        return False
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return evidence_lineups_available((payload.get("match") or {}).get("lineups"))


def evidence_lineups_available(lineups) -> bool:
    if not isinstance(lineups, dict) or len(lineups) < 2:
        return False
    return all(
        len((team or {}).get("starting_xi") or []) >= 11
        for team in lineups.values()
    )


def _read_marker(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _resolve_path(path: Path) -> Path:
    if path.exists():
        return path
    if path.is_absolute() and path.parts[:2] == ("/", "app"):
        return config.ROOT / Path(*path.parts[2:])
    return path
