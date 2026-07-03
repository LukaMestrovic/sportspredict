#!/usr/bin/env python3
"""Manual GPT submission bridge.

The manual path deliberately uses the same deployed container as cron. It builds
fresh evidence without calling OpenAI, accepts an audited GPT JSON response over
stdin, submits through the normal ledger/platform path, verifies SportPredict,
then writes the cron-skip marker.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import sqlite3
import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from bot import (
    config,
    evidence,
    ledger,
    lineups as lineup_fetcher,
    llm_pricing,
    parser as question_parser,
    submission_state,
)
from bot.apifootball import APIFootball
from bot.oddsapi import OddsAPI
from bot.pipeline import (
    MatchResult,
    PlatformVerificationError,
    run_match,
    submit_with_ledger,
    verify_platform_predictions,
)
from bot.sportspredict import SportPredict


MANUAL_DIR = config.ROOT / "logs" / "manual_submissions"
CRON_WINDOW = 30
MODEL_LABEL = "manual-chatgpt-gpt-5.5-extra-high"


def main() -> None:
    ap = argparse.ArgumentParser(description="Manual GPT-5.5 submission flow")
    sub = ap.add_subparsers(dest="cmd", required=True)

    status = sub.add_parser("status")
    status.add_argument("--next", action="store_true", help="show next open match")

    prep = sub.add_parser("prepare")
    prep.add_argument("--next", action="store_true", help="prepare the next open match")
    prep.add_argument("--fresh", action="store_true", help="refresh odds and lineups")
    prep.add_argument(
        "--require-lineups", action="store_true",
        help="warn unless both teams have confirmed starting XIs",
    )

    submit = sub.add_parser("submit")
    submit.add_argument("--session", required=True)
    submit.add_argument("--response-stdin", action="store_true")
    submit.add_argument("--response", help="JSON response path inside the container")

    args = ap.parse_args()
    if args.cmd == "status":
        _status(args)
    elif args.cmd == "prepare":
        _prepare(args)
    elif args.cmd == "submit":
        _submit(args)


def _status(_args) -> None:
    sp, event, lobby, match, kickoff = _next_match()
    mins = (kickoff - datetime.now(timezone.utc)).total_seconds() / 60.0
    marker = submission_state.marker_path(match["id"], kickoff, CRON_WINDOW)
    marker_with_lineups = submission_state.marker_with_lineups_exists(
        match["id"], kickoff, CRON_WINDOW,
    )
    ledger_submitted = submission_state.submitted_run_exists(
        match["id"], kickoff=match["opening_time"], lobby_id=lobby["id"],
    )
    ledger_submitted_with_lineups = (
        submission_state.submitted_run_with_lineups_exists(
            match["id"], kickoff=match["opening_time"], lobby_id=lobby["id"],
        )
    )
    latest = _latest_submitted_payload(match["id"], lobby["id"])
    verification = None
    if latest:
        verification = verify_platform_predictions(sp, lobby["id"], latest["payload"])

    print(f"MATCH={match.get('name', match['id'])}")
    print(f"MATCH_ID={match['id']}")
    print(f"KICKOFF={kickoff.isoformat()}")
    print(f"MINUTES_TO_KICKOFF={mins:.1f}")
    print(f"MARKER_PATH={_host_path(marker)}")
    print(f"MARKER_EXISTS={str(marker.exists()).lower()}")
    print(f"MARKER_WITH_LINEUPS={str(marker_with_lineups).lower()}")
    print(f"LEDGER_SUBMITTED={str(ledger_submitted).lower()}")
    print(f"LEDGER_SUBMITTED_WITH_LINEUPS={str(ledger_submitted_with_lineups).lower()}")
    if latest:
        print(f"LEDGER_RUN_ID={latest['run_id']}")
        print(f"LEDGER_EVIDENCE_PATH={_host_path(latest['evidence_path'])}")
    if verification is not None:
        print("PLATFORM_VERIFICATION=" + json.dumps(verification, sort_keys=True))


def _prepare(args) -> None:
    with _nonblocking_lock():
        sp, event, lobby, match, kickoff = _next_match()
        _refuse_if_already_done(match, kickoff, lobby["id"])
        now = datetime.now(timezone.utc)
        minutes_before = (kickoff - now).total_seconds() / 60.0
        if minutes_before <= 0:
            raise SystemExit("kickoff has passed; refusing manual prepare")

        af = APIFootball(refresh_odds=args.fresh)
        fixture = af.find_fixture(match["opening_time"], match.get("name"))
        if not fixture:
            raise SystemExit("no API-Football fixture found")
        lineups = lineup_fetcher.fetch_lineups(af, fixture, refresh=args.fresh)
        lineups_available = _confirmed_lineups(lineups)
        lineup_warning = None
        if args.require_lineups and not lineups_available:
            lineup_warning = (
                "confirmed API-Football/FIFA lineups are unavailable; "
                "evidence will omit lineups and the manual GPT run must research "
                "official/published lineup or team-news context on the web"
            )

        oa = OddsAPI(refresh_odds=args.fresh)
        markets = sp.markets(lobby["id"], match["id"])
        with _disable_openai_parser_calls():
            result = run_match(
                match, markets, af, oa,
                llm_pricing_enabled=True,
                llm_pricing_refresh=False,
                llm_pricing_call=False,
                lineups=lineups,
                minutes_before=minutes_before,
            )
        if not result.evidence_json or not result.evidence_path:
            raise SystemExit("manual prepare failed to build evidence")

        MANUAL_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        slug = _slug(match.get("name") or f"{result.home}_vs_{result.away}")
        chatgpt_path = MANUAL_DIR / f"{stamp}_{slug}_chatgpt_request.md"
        session_path = MANUAL_DIR / f"{stamp}_{slug}_session.json"
        response_path = MANUAL_DIR / f"{stamp}_{slug}_response.json"

        session = _session_payload(
            event, lobby, match, fixture, markets, result, minutes_before,
            chatgpt_path, response_path, lineups_available, lineup_warning,
        )
        session_path.write_text(
            json.dumps(session, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        )
        chatgpt_path.write_text(
            _chatgpt_request(result.evidence_json, result.evidence_path),
            encoding="utf-8",
        )

        print(f"MATCH={match.get('name', match['id'])}")
        print(f"MATCH_ID={match['id']}")
        print(f"KICKOFF={kickoff.isoformat()}")
        print(f"MINUTES_TO_KICKOFF={minutes_before:.1f}")
        print(f"LINEUPS_AVAILABLE={str(lineups_available).lower()}")
        if lineup_warning:
            print(f"LINEUP_WARNING={lineup_warning}")
        print(f"SESSION_PATH={_container_path(session_path)}")
        print(f"SESSION_HOST_PATH={_host_path(session_path)}")
        print(f"EVIDENCE_PATH={_container_path(result.evidence_path)}")
        print(f"EVIDENCE_HOST_PATH={_host_path(result.evidence_path)}")
        print(f"CHATGPT_REQUEST_PATH={_host_path(chatgpt_path)}")
        print(f"CHATGPT_REQUEST_CONTAINER_PATH={_container_path(chatgpt_path)}")
        print(f"RESPONSE_PATH={_container_path(response_path)}")
        print(f"RESPONSE_HOST_PATH={_host_path(response_path)}")


def _submit(args) -> None:
    if not (args.response_stdin or args.response):
        raise SystemExit("provide --response-stdin or --response")
    with _nonblocking_lock():
        session_path = Path(args.session)
        session = json.loads(session_path.read_text(encoding="utf-8"))
        kickoff = _parse_kickoff(session["match"]["opening_time"])
        _refuse_if_already_done(session["match"], kickoff, session["lobby_id"])
        if (kickoff - datetime.now(timezone.utc)).total_seconds() <= 0:
            raise SystemExit("kickoff has passed; refusing manual submit")

        response_text = (
            sys.stdin.read() if args.response_stdin
            else Path(args.response).read_text(encoding="utf-8")
        )
        response = llm_pricing._extract_json(response_text)  # strict schema follows.
        response_path = Path(session["response_path"])
        response_path.write_text(
            json.dumps(response, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

        evidence_path = Path(session["evidence_path"])
        evidence_json = json.loads(evidence_path.read_text(encoding="utf-8"))
        result = _result_from_session(session)
        llm_pricing.apply_pricing_response(
            result, evidence_json, evidence_path, response,
            require_all_markets=True,
            model_label=MODEL_LABEL,
        )
        sp = SportPredict()
        try:
            outcome, run_ids = submit_with_ledger(
                sp, session["event_id"], session["lobby_id"], [result],
                window_min=int(round(session["minutes_before"])),
                minutes_before=float(session["minutes_before"]),
            )
        except PlatformVerificationError as exc:
            print("PLATFORM_VERIFICATION=" + json.dumps(exc.verification, sort_keys=True))
            raise

        verification = outcome.get("platform_verification")
        if not verification or not verification.get("ok"):
            raise SystemExit(
                "SportPredict platform verification did not succeed; marker not written"
            )

        run_id = run_ids[0]
        submitted_with_lineups = bool(session.get("lineups_available")) or _result_has_lineups(result)
        marker = submission_state.write_marker(
            session["match"]["id"], kickoff, CRON_WINDOW,
            source="manual-chatgpt",
            metadata={
                "ledger_run_id": run_id,
                "evidence_hash": result.evidence_hash,
                "evidence_path": result.evidence_path,
                "lineups_available": submitted_with_lineups,
                "platform_verification": verification,
            },
        )
        print(f"MATCH={session['match'].get('name', session['match']['id'])}")
        print(f"KICKOFF={kickoff.isoformat()}")
        print(f"EVIDENCE_PATH={_host_path(result.evidence_path)}")
        if result.llm_match_read_path:
            print(f"MATCH_READ_PATH={_host_path(result.llm_match_read_path)}")
        print(f"LLM_AUDIT_PATH={_host_path(result.llm_pricing_audit_path)}")
        print(f"LLM_REPORT_PATH={_host_path(result.llm_pricing_report_path)}")
        print(f"LEDGER_RUN_ID={run_id}")
        print("SUBMISSION_OUTCOME=" + json.dumps({
            key: outcome[key] for key in (
                "submitted", "updated", "unchanged", "failed",
            )
        }, sort_keys=True))
        print(
            "PLATFORM_VERIFICATION="
            + json.dumps(verification, sort_keys=True)
        )
        _print_predictions(result)
        print(f"CRON_MARKER_PATH={_host_path(marker)}")
        print(f"CRON_MARKER_WITH_LINEUPS={str(submitted_with_lineups).lower()}")
        print("CRON_BLOCKED=true")


def _next_match():
    sp = SportPredict()
    event = sp.event()
    lobby = sp.lobby(event["id"])
    now = datetime.now(timezone.utc)
    upcoming = [
        (m, _parse_kickoff(m["opening_time"]))
        for m in sp.matches(event["id"], lobby["id"])
    ]
    upcoming = sorted(((m, k) for m, k in upcoming if k > now),
                      key=lambda item: item[1])
    if not upcoming:
        raise SystemExit("no upcoming open matches")
    match, kickoff = upcoming[0]
    return sp, event, lobby, match, kickoff


def _refuse_if_already_done(match: dict, kickoff: datetime, lobby_id: str) -> None:
    marker = submission_state.marker_path(match["id"], kickoff, CRON_WINDOW)
    if submission_state.marker_exists(match["id"], kickoff, CRON_WINDOW):
        raise SystemExit(
            f"submission marker already exists: {_host_path(marker)}"
        )
    if submission_state.submitted_run_exists(
        match["id"], kickoff=match["opening_time"], lobby_id=lobby_id,
    ):
        raise SystemExit("submitted ledger run already exists")


def _confirmed_lineups(lineups: list[dict] | None) -> bool:
    summary = evidence.summarize_lineups(lineups)
    if not summary or len(summary) < 2:
        return False
    return all(len((team or {}).get("starting_xi") or []) >= 11
               for team in summary.values())


@contextmanager
def _disable_openai_parser_calls():
    old_config_key = config.OPENAI_API_KEY
    old_parser_key = question_parser.config.OPENAI_API_KEY
    config.OPENAI_API_KEY = ""
    question_parser.config.OPENAI_API_KEY = ""
    try:
        yield
    finally:
        config.OPENAI_API_KEY = old_config_key
        question_parser.config.OPENAI_API_KEY = old_parser_key


@contextmanager
def _nonblocking_lock():
    submission_state.LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(submission_state.LOCK_PATH, "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit("another cron/manual submission run is in progress")
        yield


def _session_payload(
    event: dict,
    lobby: dict,
    match: dict,
    fixture: dict,
    markets: list[dict],
    result: MatchResult,
    minutes_before: float,
    chatgpt_path: Path,
    response_path: Path,
    lineups_available: bool,
    lineup_warning: str | None,
) -> dict:
    return {
        "schema_version": 1,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "event_id": event["id"],
        "lobby_id": lobby["id"],
        "match": match,
        "fixture": fixture,
        "home": result.home,
        "away": result.away,
        "minutes_before": round(minutes_before, 1),
        "lineups_available": lineups_available,
        "lineup_warning": lineup_warning,
        "cron_marker_window": CRON_WINDOW,
        "markets": markets,
        "intents": result.intents,
        "market_specs": result.market_specs,
        "skip_reasons": result.skip_reasons,
        "af_books": result.af_books,
        "oa_observations": result.oa_observations,
        "evidence_path": _container_path(result.evidence_path),
        "evidence_hash": result.evidence_hash,
        "chatgpt_request_path": _container_path(chatgpt_path),
        "response_path": _container_path(response_path),
    }


def _result_from_session(session: dict) -> MatchResult:
    return MatchResult(
        sp_match=session["match"],
        fixture=session.get("fixture"),
        home=session.get("home"),
        away=session.get("away"),
        markets=session.get("markets") or [],
        intents=session.get("intents") or {},
        market_specs=session.get("market_specs") or {},
        skip_reasons=session.get("skip_reasons") or {},
        af_books=session.get("af_books") or [],
        oa_observations=session.get("oa_observations") or [],
        evidence_path=session.get("evidence_path"),
        evidence_hash=session.get("evidence_hash"),
        llm_match_read_path=session.get("llm_match_read_path"),
    )


def _chatgpt_request(evidence_json: dict, evidence_path: str) -> str:
    lineups = (evidence_json.get("match") or {}).get("lineups")
    lineup_note = (
        "Confirmed provider lineups are present in `match.lineups`."
        if lineups else
        "Confirmed API-Football/FIFA lineups are not present in this evidence. "
        "Use pre-kickoff web research for official lineups or team news where "
        "available, and otherwise price with explicit lineup uncertainty."
    )
    return (
        "# Manual SportPredict GPT-5.5 request\n\n"
        "Use GPT-5.5 with extra-high reasoning for your own research and "
        "judgement. Do not call the OpenAI API. Return only the JSON object "
        "specified by the pricing prompt.\n\n"
        f"- Evidence container path: `{_container_path(evidence_path)}`\n"
        f"- Evidence host path: `{_host_path(evidence_path)}`\n\n"
        f"Lineup note: {lineup_note}\n\n"
        "## Manual instruction\n\n"
        "Execute `prompts/llm_pricing_prompt.md` as the full task specification. "
        "Use the provided evidence JSON as MATCH EVIDENCE JSON. This request is "
        "designed for one prompt-only main agent that first prices base "
        "probabilities, then creates an extensive match-read markdown through "
        "aspect subagents/emulated aspect passes, then performs one "
        "question-specific adjustment pass per `question_evidence` item. Use "
        "each item's `question_id`, `decision_basis`, and `subagent_brief` as "
        "the delegation packet.\n\n"
        "Run pre-kickoff web research where required. Return ONLY valid JSON "
        "matching the prompt output schema: top-level `briefing`, `sources`, "
        "`match_read_markdown`, `match_read_sources`, and `markets`. Include "
        "one market object for every `market_id` in `question_evidence`, with "
        "`base_probability_int` and `language_adjustment` on every market.\n\n"
        "Do not include prose outside JSON. Do not reveal private chain-of-"
        "thought; keep reasoning in concise public audit summaries.\n\n"
        "## prompts/llm_pricing_prompt.md\n\n"
        "```text\n"
        f"{llm_pricing._load_prompt()}\n"
        "```\n\n"
        "## MATCH EVIDENCE JSON\n\n"
        "```json\n"
        f"{json.dumps(evidence_json, ensure_ascii=False, indent=2)}\n"
        "```\n"
    )


def _latest_submitted_payload(match_id: str, lobby_id: str) -> dict | None:
    if not ledger.LEDGER_PATH.exists():
        return None
    with ledger.connect() as db:
        db.row_factory = sqlite3.Row
        run = db.execute(
            """SELECT id, evidence_path
               FROM runs
               WHERE match_id = ? AND lobby_id = ? AND status = 'submitted'
               ORDER BY submitted_at DESC, recorded_at DESC
               LIMIT 1""",
            (match_id, lobby_id),
        ).fetchone()
        if not run:
            return None
        questions = db.execute(
            """SELECT market_id, probability_int
               FROM questions
               WHERE run_id = ? AND probability_int IS NOT NULL""",
            (run["id"],),
        ).fetchall()
    return {
        "run_id": run["id"],
        "evidence_path": run["evidence_path"],
        "payload": [
            {"market_id": q["market_id"], "lobby_id": lobby_id,
             "probability": q["probability_int"]}
            for q in questions
        ],
    }


def _print_predictions(result: MatchResult) -> None:
    rows = [
        {
            "market_id": p.market_id,
            "probability_int": p.probability_int,
            "question": p.question,
            "reasoning_summary": p.llm_reasoning_summary,
        }
        for p in result.predictions
    ]
    print("PREDICTIONS_JSON=" + json.dumps(rows, ensure_ascii=False, sort_keys=True))
    print("PREDICTIONS:")
    for row in rows:
        reason = row["reasoning_summary"] or ""
        suffix = f" — {reason}" if reason else ""
        print(
            f"- {row['probability_int']:>2}%  {row['market_id']}  "
            f"{row['question']}{suffix}"
        )


def _result_has_lineups(result: MatchResult) -> bool:
    evidence_json = getattr(result, "evidence_json", None) or {}
    return submission_state.evidence_has_lineups(
        getattr(result, "evidence_path", None)
    ) or submission_state.evidence_lineups_available(
        (evidence_json.get("match") or {}).get("lineups")
    )


def _parse_kickoff(opening_time: str) -> datetime:
    dt = datetime.fromisoformat(opening_time.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _host_path(pathish) -> str:
    path = Path(str(pathish))
    host_root = os.environ.get("SPLLM_HOST_ROOT")
    if not host_root:
        return str(path)
    try:
        rel = path.relative_to(config.ROOT)
    except ValueError:
        return str(path)
    return str(Path(host_root) / rel)


def _container_path(pathish) -> str:
    path = Path(str(pathish))
    host_root = os.environ.get("SPLLM_HOST_ROOT")
    if host_root:
        try:
            rel = path.relative_to(Path(host_root))
            return str(config.ROOT / rel)
        except ValueError:
            pass
    return str(path)


def _slug(value: str) -> str:
    import re

    return re.sub(r"[^A-Za-z0-9]+", "_", value).strip("_").lower()[:80] or "match"


if __name__ == "__main__":
    main()
