"""Durable prediction ledger and real-result settlement storage."""
from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

from . import config
from .parser import PROMPT_VERSION


LEDGER_PATH = config.ROOT / "logs" / "prediction_ledger.sqlite3"
SCHEMA_VERSION = 1


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def connect(path: Path = LEDGER_PATH) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    db.executescript("""
        CREATE TABLE IF NOT EXISTS runs (
            id TEXT PRIMARY KEY,
            recorded_at TEXT NOT NULL,
            event_id TEXT NOT NULL,
            lobby_id TEXT NOT NULL,
            match_id TEXT NOT NULL,
            fixture_id INTEGER,
            match_name TEXT NOT NULL,
            home TEXT,
            away TEXT,
            kickoff TEXT NOT NULL,
            window_min INTEGER NOT NULL,
            minutes_before REAL NOT NULL,
            parser_version TEXT NOT NULL,
            parser_model TEXT NOT NULL,
            status TEXT NOT NULL,
            submitted_at TEXT,
            error TEXT,
            af_odds_json TEXT NOT NULL,
            oa_odds_json TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS questions (
            run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
            market_id TEXT NOT NULL,
            question TEXT NOT NULL,
            intent_json TEXT,
            market_spec_json TEXT,
            probability REAL,
            probability_int INTEGER,
            source TEXT,
            n_books INTEGER,
            market_label TEXT,
            skip_reason TEXT,
            outcome INTEGER,
            brier_score REAL,
            settled_at TEXT,
            result_id TEXT,
            result_created_at TEXT,
            PRIMARY KEY (run_id, market_id)
        );

        CREATE INDEX IF NOT EXISTS questions_market_id
            ON questions(market_id);
        CREATE INDEX IF NOT EXISTS questions_unsettled
            ON questions(outcome) WHERE outcome IS NULL;
    """)
    db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    return db


def record_run(
    event_id: str,
    lobby_id: str,
    result,
    window_min: int,
    minutes_before: float,
    *,
    path: Path = LEDGER_PATH,
    run_id: str | None = None,
    recorded_at: str | None = None,
) -> str:
    """Persist one priced match, including skipped questions and raw inputs."""
    run_id = run_id or str(uuid.uuid4())
    recorded_at = recorded_at or _now()
    fixture_id = (
        result.fixture.get("fixture", {}).get("id") if result.fixture else None
    )
    predictions = {p.market_id: p for p in result.predictions}

    with connect(path) as db:
        db.execute(
            """INSERT INTO runs (
                id, recorded_at, event_id, lobby_id, match_id, fixture_id,
                match_name, home, away, kickoff, window_min, minutes_before,
                parser_version, parser_model, status, af_odds_json, oa_odds_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'priced', ?, ?)""",
            (
                run_id, recorded_at, event_id, lobby_id, result.sp_match["id"],
                fixture_id, result.sp_match.get("name", result.sp_match["id"]),
                result.home, result.away, result.sp_match["opening_time"],
                window_min, minutes_before, PROMPT_VERSION, config.PARSER_MODEL,
                _json(result.af_books), _json(result.oa_observations),
            ),
        )
        for market in result.markets:
            market_id = market["id"]
            prediction = predictions.get(market_id)
            db.execute(
                """INSERT INTO questions (
                    run_id, market_id, question, intent_json, market_spec_json,
                    probability, probability_int, source, n_books, market_label,
                    skip_reason
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    run_id, market_id, market["question"],
                    _json(result.intents[market_id])
                    if market_id in result.intents else None,
                    _json(result.market_specs[market_id])
                    if result.market_specs.get(market_id) is not None else None,
                    prediction.probability if prediction else None,
                    prediction.probability_int if prediction else None,
                    prediction.source if prediction else None,
                    prediction.n_books if prediction else None,
                    prediction.market_label if prediction else None,
                    result.skip_reasons.get(market_id),
                ),
            )
    return run_id


def mark_submitted(
    run_id: str, *, path: Path = LEDGER_PATH, submitted_at: str | None = None
) -> None:
    with connect(path) as db:
        db.execute(
            "UPDATE runs SET status = 'submitted', submitted_at = ?, error = NULL "
            "WHERE id = ?",
            (submitted_at or _now(), run_id),
        )


def mark_failed(run_id: str, error: str, *, path: Path = LEDGER_PATH) -> None:
    with connect(path) as db:
        db.execute(
            "UPDATE runs SET status = 'failed', error = ? WHERE id = ?",
            (error, run_id),
        )
