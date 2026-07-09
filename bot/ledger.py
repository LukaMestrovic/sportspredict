"""Durable prediction ledger and real-result settlement storage."""
from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from . import config, parser as question_parser


LEDGER_PATH = config.ROOT / "logs" / "prediction_ledger.sqlite3"
SCHEMA_VERSION = 9


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _ensure_columns(
    db: sqlite3.Connection, table: str, columns: tuple[tuple[str, str], ...]
) -> None:
    existing = {row[1] for row in db.execute(f"PRAGMA table_info({table})").fetchall()}
    for name, declaration in columns:
        if name not in existing:
            db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {declaration}")


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
            oa_odds_json TEXT NOT NULL,
            evidence_path TEXT,
            evidence_hash TEXT,
            llm_match_read_path TEXT,
            llm_pricing_audit_path TEXT,
            llm_pricing_report_path TEXT,
            llm_pricing_briefing_json TEXT,
            session_id TEXT,
            session_manifest_path TEXT
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
            book_probabilities_json TEXT,
            market_label TEXT,
            skip_reason TEXT,
            llm_audit_json TEXT,
            llm_sources_json TEXT,
            llm_reasoning_summary TEXT,
            intent_source TEXT,
            intent_resolution_json TEXT,
            outcome INTEGER,
            brier_score REAL,
            settled_at TEXT,
            result_id TEXT,
            result_created_at TEXT,
            result_probability_int INTEGER,
            result_brier_score REAL,
            PRIMARY KEY (run_id, market_id)
        );

        CREATE INDEX IF NOT EXISTS questions_market_id
            ON questions(market_id);
        CREATE INDEX IF NOT EXISTS questions_unsettled
            ON questions(outcome) WHERE outcome IS NULL;
    """)
    _ensure_columns(db, "questions", (
        ("result_probability_int", "INTEGER"),
        ("result_brier_score", "REAL"),
        ("book_probabilities_json", "TEXT"),
        ("llm_audit_json", "TEXT"),
        ("llm_sources_json", "TEXT"),
        ("llm_reasoning_summary", "TEXT"),
        ("intent_source", "TEXT"),
        ("intent_resolution_json", "TEXT"),
    ))
    _ensure_columns(db, "runs", (
        ("evidence_path", "TEXT"),
        ("evidence_hash", "TEXT"),
        ("llm_match_read_path", "TEXT"),
        ("llm_pricing_audit_path", "TEXT"),
        ("llm_pricing_report_path", "TEXT"),
        ("llm_pricing_briefing_json", "TEXT"),
        ("session_id", "TEXT"),
        ("session_manifest_path", "TEXT"),
    ))
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

    with closing(connect(path)) as db, db:
        codex_briefing = getattr(result, "codex_briefing", None)
        codex_pricing_json = _json({
            "briefing": codex_briefing,
            "sources": getattr(result, "codex_sources", []),
        }) if codex_briefing else None
        db.execute(
            """INSERT INTO runs (
                id, recorded_at, event_id, lobby_id, match_id, fixture_id,
                match_name, home, away, kickoff, window_min, minutes_before,
                parser_version, parser_model, status, af_odds_json, oa_odds_json,
                evidence_path, evidence_hash, llm_match_read_path,
                llm_pricing_audit_path, llm_pricing_report_path,
                llm_pricing_briefing_json, session_id, session_manifest_path
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'priced',
                      ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                run_id, recorded_at, event_id, lobby_id, result.sp_match["id"],
                fixture_id, result.sp_match.get("name", result.sp_match["id"]),
                result.home, result.away, result.sp_match["opening_time"],
                window_min, minutes_before,
                getattr(question_parser, "PARSER_SCHEMA_VERSION", "deterministic-v1"),
                "deterministic",
                _json(result.af_books), _json(result.oa_observations),
                getattr(result, "evidence_path", None),
                getattr(result, "evidence_hash", None),
                getattr(result, "codex_match_read_path", None),
                getattr(result, "codex_audit_path", None),
                getattr(result, "codex_report_path", None),
                codex_pricing_json,
                getattr(result, "session_id", None),
                getattr(result, "session_manifest_path", None),
            ),
        )
        for market in result.markets:
            market_id = market["id"]
            prediction = predictions.get(market_id)
            db.execute(
                """INSERT INTO questions (
                    run_id, market_id, question, intent_json, market_spec_json,
                    probability, probability_int, source, n_books, market_label,
                    book_probabilities_json, skip_reason,
                    llm_audit_json, llm_sources_json, llm_reasoning_summary,
                    intent_source, intent_resolution_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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
                    _json(prediction.book_probabilities) if prediction else None,
                    result.skip_reasons.get(market_id),
                    _json(getattr(prediction, "codex_audit", {}))
                    if prediction and getattr(prediction, "codex_audit", None)
                    else None,
                    _json(getattr(prediction, "codex_sources", []))
                    if prediction else None,
                    getattr(prediction, "codex_reasoning_summary", None)
                    if prediction else None,
                    getattr(result, "intent_sources", {}).get(market_id),
                    _json(getattr(result, "intent_resolutions", {}).get(market_id))
                    if getattr(result, "intent_resolutions", {}).get(market_id)
                    is not None else None,
                ),
            )
    return run_id


def mark_submitted(
    run_id: str, *, path: Path = LEDGER_PATH, submitted_at: str | None = None
) -> None:
    with closing(connect(path)) as db, db:
        db.execute(
            "UPDATE runs SET status = 'submitted', submitted_at = ?, error = NULL "
            "WHERE id = ?",
            (submitted_at or _now(), run_id),
        )


def mark_failed(run_id: str, error: str, *, path: Path = LEDGER_PATH) -> None:
    with closing(connect(path)) as db, db:
        db.execute(
            "UPDATE runs SET status = 'failed', error = ? WHERE id = ?",
            (error, run_id),
        )


def unsettled_match_ids(
    lobby_id: str, *, path: Path = LEDGER_PATH
) -> list[str]:
    with closing(connect(path)) as db, db:
        rows = db.execute(
            """SELECT DISTINCT r.match_id
               FROM runs r JOIN questions q ON q.run_id = r.id
               WHERE r.lobby_id = ? AND r.status = 'submitted'
                 AND q.probability_int IS NOT NULL AND q.outcome IS NULL
               ORDER BY r.kickoff""",
            (lobby_id,),
        ).fetchall()
    return [row["match_id"] for row in rows]


def settle_results(
    outcomes: dict[str, int],
    results: list[dict],
    *,
    path: Path = LEDGER_PATH,
    settled_at: str | None = None,
) -> dict[str, int]:
    """Settle submitted ledger rows by stable SportPredict market ID.

    ``outcomes`` comes from the settled web API's explicit ``current_value``;
    the authenticated results payload supplies the official final submission.
    """
    settled_at = settled_at or _now()
    result_by_market = {
        row["market_id"]: row for row in results
        if row.get("market_status") == "settled"
    }
    updated = 0
    with closing(connect(path)) as db, db:
        for market_id, outcome in outcomes.items():
            if outcome not in (0, 1):
                continue
            result = result_by_market.get(market_id, {})
            rows = db.execute(
                """SELECT q.run_id, q.probability_int
                   FROM questions q JOIN runs r ON r.id = q.run_id
                   WHERE q.market_id = ? AND q.outcome IS NULL
                     AND q.probability_int IS NOT NULL
                     AND r.status = 'submitted'""",
                (market_id,),
            ).fetchall()
            for row in rows:
                probability = row["probability_int"] / 100.0
                brier = (probability - outcome) ** 2
                db.execute(
                    """UPDATE questions SET
                        outcome = ?, brier_score = ?,
                        settled_at = ?, result_id = ?, result_created_at = ?,
                        result_probability_int = ?, result_brier_score = ?
                       WHERE run_id = ? AND market_id = ?""",
                    (
                        outcome, brier, settled_at,
                        result.get("id"),
                        result.get("created_date"),
                        result.get("probability_submitted"),
                        result.get("brier_score"), row["run_id"], market_id,
                    ),
                )
                updated += 1
        remaining = db.execute(
            """SELECT COUNT(*) FROM questions q
               JOIN runs r ON r.id = q.run_id
               WHERE r.status = 'submitted' AND q.probability_int IS NOT NULL
                 AND q.outcome IS NULL"""
        ).fetchone()[0]
    return {"settled_predictions": updated, "remaining_predictions": remaining}


def performance(*, path: Path = LEDGER_PATH) -> list[dict]:
    """Return overall and per-window/source Brier summaries."""
    queries = (
        ("overall", "'all'", "'all'"),
        ("window", "CAST(r.window_min AS TEXT)", "'all'"),
        ("source", "'all'", "q.source"),
    )
    summaries: list[dict] = []
    with closing(connect(path)) as db, db:
        for group, window_expr, source_expr in queries:
            rows = db.execute(
                f"""SELECT {window_expr} AS window_min,
                            {source_expr} AS source,
                            COUNT(*) AS predictions,
                            AVG(q.brier_score) AS mean_brier
                     FROM questions q JOIN runs r ON r.id = q.run_id
                     WHERE q.outcome IS NOT NULL
                     GROUP BY {window_expr}, {source_expr}
                     ORDER BY window_min, source"""
            ).fetchall()
            summaries.extend({"group": group, **dict(row)} for row in rows)
    return summaries


def match_detail(query: str, *, path: Path = LEDGER_PATH) -> dict:
    """Latest submitted run for a match (by match_id or name substring).

    Returns ``{"run": {...}, "questions": [...]}`` for post-match review of every
    prediction and its Codex pricing reasoning, or ``{}`` if none is found.
    """
    with closing(connect(path)) as db, db:
        like = f"%{query}%"
        run = db.execute(
            """SELECT * FROM runs
               WHERE (match_id = ? OR match_name LIKE ? OR home LIKE ? OR away LIKE ?)
                 AND status = 'submitted'
               ORDER BY recorded_at DESC LIMIT 1""",
            (query, like, like, like),
        ).fetchone()
        if run is None:
            return {}
        rows = db.execute(
            "SELECT * FROM questions WHERE run_id = ? ORDER BY market_id",
            (run["id"],),
        ).fetchall()
    return {"run": dict(run), "questions": [dict(r) for r in rows]}
