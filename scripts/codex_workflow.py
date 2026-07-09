#!/usr/bin/env python3
"""Manual Codex prepare, intent-resolution, and submission workflow."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

from bot import codex_pricing, evidence, intent_resolution, ledger
from bot import lineups as lineup_fetcher
from bot import parser as question_parser
from bot.apifootball import APIFootball
from bot.oddsapi import OddsAPI
from bot.operation_lock import operation_lock
from bot.pipeline import (
    MatchResult,
    PlatformVerificationError,
    prepare_match,
    submit_with_ledger,
    verify_platform_predictions,
)
from bot.sportspredict import SportPredict


RUNS_DIR = Path(__file__).resolve().parent.parent / "logs" / "codex_runs"
MANIFEST_SCHEMA_VERSION = 2
PROVIDER_SNAPSHOT_SCHEMA_VERSION = 1
MODEL_LABEL = "manual-codex"


def main() -> None:
    parser = argparse.ArgumentParser(description="Manual Codex prediction workflow")
    sub = parser.add_subparsers(dest="cmd", required=True)

    status = sub.add_parser("status", help="inspect one upcoming match")
    _add_selector(status)
    status.add_argument("--fresh", action="store_true", help="refresh lineup lookup")

    prepare = sub.add_parser("prepare", help="build a deterministic Codex handoff")
    _add_selector(prepare)
    prepare.add_argument("--fresh", action="store_true", help="refresh odds and lineups once")

    resume = sub.add_parser("resume", help="accept new intents and finish preparation")
    resume.add_argument("--request", required=True, help="intent_request.json path")
    resume.add_argument("--intents", required=True, help="Codex intent response JSON path")
    resume.add_argument("--fresh", action="store_true", help="refresh odds and lineups once")

    submit = sub.add_parser("submit", help="validate and submit a Codex response")
    submit.add_argument("--session", required=True, help="manifest or legacy session JSON")
    response = submit.add_mutually_exclusive_group(required=True)
    response.add_argument("--response", help="Codex pricing response JSON path")
    response.add_argument("--response-stdin", action="store_true")

    args = parser.parse_args()
    if args.cmd == "status":
        _status(args)
    elif args.cmd == "prepare":
        _with_lock(_prepare, args)
    elif args.cmd == "resume":
        _with_lock(_resume, args)
    else:
        _with_lock(_submit, args)


def _add_selector(command: argparse.ArgumentParser) -> None:
    selector = command.add_mutually_exclusive_group(required=True)
    selector.add_argument("--next", action="store_true", help="select the next open match")
    selector.add_argument("--match", help="exact match ID or unique name substring")


def _with_lock(function, args) -> None:
    try:
        with operation_lock():
            function(args)
    except RuntimeError as exc:
        raise SystemExit(str(exc)) from exc


def _status(args) -> None:
    sp, event, lobby, match, kickoff = _select_match(
        next_match=args.next, query=args.match,
    )
    minutes = _minutes_before(kickoff)
    fixture = None
    lineups = None
    lineup_error = None
    try:
        af = APIFootball(refresh_odds=args.fresh)
        fixture = af.find_fixture(match["opening_time"], match.get("name"))
        if fixture:
            lineups = lineup_fetcher.fetch_lineups(af, fixture, refresh=args.fresh)
    except Exception as exc:  # status remains useful when a context provider is down
        lineup_error = str(exc)
    lineup_available = _confirmed_lineups(lineups)
    latest = _latest_submitted_payload(match["id"], lobby["id"])
    verification = (
        verify_platform_predictions(sp, lobby["id"], latest["payload"])
        if latest else None
    )

    print("STATUS=ready")
    _print_match(match, kickoff, minutes)
    print(f"LINEUPS_AVAILABLE={str(lineup_available).lower()}")
    if lineup_error:
        print(f"LINEUP_WARNING={lineup_error}")
    if latest:
        print(f"LEDGER_RUN_ID={latest['run_id']}")
        print(f"LEDGER_EVIDENCE_PATH={_host_path(latest['evidence_path'])}")
    if verification is not None:
        print("PLATFORM_VERIFICATION=" + json.dumps(verification, sort_keys=True))


def _prepare(args) -> None:
    sp, event, lobby, match, kickoff = _select_match(
        next_match=args.next, query=args.match,
    )
    if _minutes_before(kickoff) <= 0:
        raise SystemExit("kickoff has passed; refusing manual prepare")
    markets = sp.markets(lobby["id"], match["id"])
    af = APIFootball(refresh_odds=args.fresh)
    fixture = af.find_fixture(match["opening_time"], match.get("name"))
    if not fixture:
        raise SystemExit("no API-Football fixture found")
    home = fixture["teams"]["home"]["name"]
    away = fixture["teams"]["away"]["name"]
    parsed = question_parser.parse_questions(markets, home, away)
    run_dir, session_id = _new_run_directory(match)

    if parsed.unresolved:
        request = intent_resolution.build_resolution_request(
            match_id=match["id"],
            kickoff=match["opening_time"],
            home=home,
            away=away,
            questions=markets,
            unresolved=parsed.unresolved,
        )
        request_path = run_dir / "intent_request.json"
        response_path = run_dir / "intent_response.json"
        task_path = run_dir / "intent_task.md"
        _write_json(request_path, request)
        task_path.write_text(
            _intent_task(request_path, response_path, request), encoding="utf-8",
        )
        print("STATUS=needs_intents")
        _print_match(match, kickoff, _minutes_before(kickoff))
        print(f"SESSION_ID={session_id}")
        _print_path("INTENT_REQUEST", request_path)
        _print_path("INTENT_TASK", task_path)
        _print_path("INTENT_RESPONSE", response_path)
        print(
            "RESUME_COMMAND=cache/deployed/run.sh manual resume "
            f"--request {_host_path(request_path)} --intents {_host_path(response_path)}"
        )
        return

    _finish_prepare(
        sp=sp, event=event, lobby=lobby, match=match, kickoff=kickoff,
        markets=markets, af=af, fixture=fixture, parsed=parsed,
        run_dir=run_dir, session_id=session_id, fresh=args.fresh,
    )


def _resume(args) -> None:
    request_path = Path(_container_path(args.request))
    response_path = Path(_container_path(args.intents))
    request = json.loads(request_path.read_text(encoding="utf-8"))
    response = json.loads(response_path.read_text(encoding="utf-8"))
    request = intent_resolution.validate_resolution_request(request)
    validated_response = intent_resolution.validate_resolution_response(
        request, response,
    )

    sp, event, lobby, match, kickoff = _select_match(
        next_match=False, query=request["match"]["id"],
    )
    if _minutes_before(kickoff) <= 0:
        raise SystemExit("kickoff has passed; refusing manual resume")
    markets = sp.markets(lobby["id"], match["id"])
    if intent_resolution.question_set_hash(markets) != request["question_set_hash"]:
        raise SystemExit("SportPredict questions changed after the intent request; prepare again")

    af = APIFootball(refresh_odds=args.fresh)
    fixture = af.find_fixture(match["opening_time"], match.get("name"))
    if not fixture:
        raise SystemExit("no API-Football fixture found")
    home = fixture["teams"]["home"]["name"]
    away = fixture["teams"]["away"]["name"]
    if home != request["match"]["home"] or away != request["match"]["away"]:
        raise SystemExit("fixture teams changed after the intent request; prepare again")
    accepted = intent_resolution.install_resolution_response(
        request, validated_response,
    )
    parsed = question_parser.parse_questions(markets, home, away)
    if parsed.unresolved:
        raise SystemExit("accepted intent response did not resolve every question")

    canonical_response_path = request_path.parent / "intent_response.json"
    _write_json(
        canonical_response_path,
        validated_response,
    )
    session_id = request_path.parent.name
    _finish_prepare(
        sp=sp, event=event, lobby=lobby, match=match, kickoff=kickoff,
        markets=markets, af=af, fixture=fixture, parsed=parsed,
        run_dir=request_path.parent, session_id=session_id, fresh=args.fresh,
        accepted_resolutions=accepted,
    )


def _finish_prepare(
    *,
    sp,
    event: dict,
    lobby: dict,
    match: dict,
    kickoff: datetime,
    markets: list[dict],
    af: APIFootball,
    fixture: dict,
    parsed,
    run_dir: Path,
    session_id: str,
    fresh: bool,
    accepted_resolutions: dict | None = None,
) -> None:
    minutes = _minutes_before(kickoff)
    lineup_error = None
    try:
        lineups = lineup_fetcher.fetch_lineups(af, fixture, refresh=fresh)
    except Exception as exc:
        lineups = []
        lineup_error = str(exc)
    lineups_available = _confirmed_lineups(lineups)
    lineup_warning = None
    if not lineups_available:
        lineup_warning = (
            "confirmed starting XIs are unavailable; Codex must research team news "
            "and price with explicit lineup uncertainty"
        )
        if lineup_error:
            lineup_warning = f"lineup lookup failed ({lineup_error}); {lineup_warning}"
    odds = OddsAPI(refresh_odds=fresh)
    result = prepare_match(
        match, markets, af, odds, lineups=lineups, minutes_before=minutes,
        evidence_directory=run_dir,
    )
    result.session_id = session_id
    result.intent_sources = dict(getattr(parsed, "intent_sources", {}))
    result.intent_resolutions = dict(
        getattr(parsed, "resolution_provenance", {})
    )
    for market_id in (accepted_resolutions or {}):
        result.intent_sources[market_id] = "manual-codex-resolution"

    provider_path = run_dir / "provider_snapshot.json"
    _write_json(provider_path, {
        "schema_version": PROVIDER_SNAPSHOT_SCHEMA_VERSION,
        "af_books": result.af_books,
        "oa_observations": result.oa_observations,
    })
    evidence_path = Path(result.evidence_path)
    task_path = run_dir / "task.md"
    prompt_path = run_dir / "prompt.md"
    manifest_path = run_dir / "manifest.json"
    response_path = run_dir / "response.json"
    prompt_path.write_text(
        codex_pricing.PROMPT_PATH.read_text(encoding="utf-8"), encoding="utf-8",
    )
    task_path.write_text(
        _pricing_task(
            session_id=session_id,
            evidence_hash=result.evidence_hash,
            manifest_path=manifest_path,
            evidence_path=evidence_path,
            prompt_path=prompt_path,
            response_path=response_path,
            lineups_available=lineups_available,
        ),
        encoding="utf-8",
    )
    manifest = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "session_id": session_id,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "event_id": event["id"],
        "lobby_id": lobby["id"],
        "match": match,
        "fixture": fixture,
        "home": result.home,
        "away": result.away,
        "minutes_before": round(minutes, 1),
        "lineups_available": lineups_available,
        "lineup_warning": lineup_warning,
        "context_error": result.context_error,
        "parser_schema_version": question_parser.PARSER_SCHEMA_VERSION,
        "evidence_schema_version": evidence.EVIDENCE_SCHEMA_VERSION,
        "codex_response_schema_version": codex_pricing.CODEX_RESPONSE_SCHEMA_VERSION,
        "intent_sources": result.intent_sources,
        "intent_resolutions": result.intent_resolutions,
        "evidence_hash": result.evidence_hash,
        "artifacts": {
            "evidence": _artifact_ref(evidence_path),
            "provider_snapshot": _artifact_ref(provider_path),
            "task": _artifact_ref(task_path),
            "prompt": _artifact_ref(prompt_path),
        },
        "response_path": str(response_path),
    }
    manifest["manifest_hash"] = _object_hash(manifest)
    _write_json(manifest_path, manifest)

    print("STATUS=prepared")
    _print_match(match, kickoff, minutes)
    print(f"SESSION_ID={session_id}")
    print(f"LINEUPS_AVAILABLE={str(lineups_available).lower()}")
    if lineup_warning:
        print(f"LINEUP_WARNING={lineup_warning}")
    _print_path("SESSION", manifest_path)
    _print_path("EVIDENCE", evidence_path)
    _print_path("CODEX_TASK", task_path)
    _print_path("RESPONSE", response_path)


def _submit(args) -> None:
    session_path = Path(_container_path(args.session))
    session = json.loads(session_path.read_text(encoding="utf-8"))
    schema_version = session.get("schema_version")
    if schema_version == MANIFEST_SCHEMA_VERSION:
        _verify_manifest_hash(session)
    elif schema_version != 1:
        raise ValueError(f"unsupported session schema_version: {schema_version!r}")
    kickoff = _parse_kickoff(session["match"]["opening_time"])
    if _minutes_before(kickoff) <= 0:
        raise SystemExit("kickoff has passed; refusing manual submit")
    response_text = (
        sys.stdin.read() if args.response_stdin
        else Path(_container_path(args.response)).read_text(encoding="utf-8")
    )
    response = codex_pricing._extract_json(response_text)

    if schema_version == MANIFEST_SCHEMA_VERSION:
        result, evidence_json, evidence_path = _result_from_manifest(session, session_path)
        prior_run = _submitted_session_run(session["session_id"])
        if prior_run:
            raise SystemExit(
                f"session already submitted in ledger run {prior_run}; prepare a new session"
            )
        run_dir = session_path.parent
        codex_pricing.apply_pricing_response(
            result, evidence_json, evidence_path, response,
            require_all_markets=True,
            model_label=MODEL_LABEL,
            expected_session_id=session["session_id"],
            expected_evidence_hash=session["evidence_hash"],
            directory=run_dir,
        )
        response_path = run_dir / "response.json"
    elif schema_version == 1:
        result, evidence_json, evidence_path = _result_from_legacy_session(session)
        codex_pricing.apply_pricing_response(
            result, evidence_json, evidence_path, response,
            require_all_markets=True, model_label=MODEL_LABEL,
        )
        response_path = Path(_container_path(
            session.get("response_path") or args.response or session_path.with_name("response.json")
        ))
    _write_json(response_path, response)

    actual_minutes_before = _minutes_before(kickoff)
    sp = SportPredict()
    try:
        outcome, run_ids = submit_with_ledger(
            sp, session["event_id"], session["lobby_id"], [result],
            window_min=int(round(actual_minutes_before)),
            minutes_before=actual_minutes_before,
        )
    except PlatformVerificationError as exc:
        print("PLATFORM_VERIFICATION=" + json.dumps(exc.verification, sort_keys=True))
        raise
    verification = outcome.get("platform_verification")
    if not verification or not verification.get("ok"):
        raise SystemExit("SportPredict platform verification did not succeed")

    print("STATUS=submitted")
    _print_match(session["match"], kickoff, _minutes_before(kickoff))
    print(f"EVIDENCE_PATH={_host_path(result.evidence_path)}")
    print(f"MATCH_READ_PATH={_host_path(result.codex_match_read_path)}")
    print(f"CODEX_AUDIT_PATH={_host_path(result.codex_audit_path)}")
    print(f"CODEX_REPORT_PATH={_host_path(result.codex_report_path)}")
    print(f"LEDGER_RUN_ID={run_ids[0]}")
    print("SUBMISSION_OUTCOME=" + json.dumps({
        key: outcome[key] for key in ("submitted", "updated", "unchanged", "failed")
    }, sort_keys=True))
    print("PLATFORM_VERIFICATION=" + json.dumps(verification, sort_keys=True))
    _print_predictions(result)


def _result_from_manifest(
    manifest: dict, manifest_path: Path,
) -> tuple[MatchResult, dict, Path]:
    required = {
        "schema_version", "session_id", "event_id", "lobby_id", "match",
        "fixture", "home", "away", "minutes_before", "evidence_hash", "artifacts",
        "manifest_hash", "parser_schema_version", "evidence_schema_version",
        "codex_response_schema_version",
    }
    missing = required - set(manifest)
    if missing:
        raise ValueError(f"manifest missing fields: {sorted(missing)}")
    if manifest["schema_version"] != MANIFEST_SCHEMA_VERSION:
        raise ValueError("unsupported manifest schema_version")
    _verify_manifest_hash(manifest)
    if manifest["parser_schema_version"] != question_parser.PARSER_SCHEMA_VERSION:
        raise ValueError("manifest parser_schema_version is stale")
    if manifest["evidence_schema_version"] != evidence.EVIDENCE_SCHEMA_VERSION:
        raise ValueError("manifest evidence_schema_version is stale")
    if (manifest["codex_response_schema_version"]
            != codex_pricing.CODEX_RESPONSE_SCHEMA_VERSION):
        raise ValueError("manifest Codex response schema_version is stale")
    if manifest["session_id"] != manifest_path.parent.name:
        raise ValueError("manifest session_id does not match its run directory")
    artifacts = manifest["artifacts"]
    if not isinstance(artifacts, dict):
        raise ValueError("manifest artifacts must be an object")
    for name in ("evidence", "provider_snapshot", "task", "prompt"):
        _verify_artifact(artifacts.get(name), name)
    evidence_path = Path(_container_path(artifacts["evidence"]["path"]))
    provider_path = Path(_container_path(artifacts["provider_snapshot"]["path"]))
    evidence_json = json.loads(evidence_path.read_text(encoding="utf-8"))
    if evidence_json.get("schema_version") != evidence.EVIDENCE_SCHEMA_VERSION:
        raise ValueError("evidence schema_version is stale")
    computed_evidence_hash = evidence.evidence_hash(evidence_json)
    if evidence_json.get("evidence_hash") != computed_evidence_hash:
        raise ValueError("evidence JSON content does not match its evidence_hash")
    if evidence_json.get("evidence_hash") != manifest["evidence_hash"]:
        raise ValueError("manifest evidence_hash does not match evidence JSON")
    evidence_match = evidence_json.get("match") or {}
    expected_match = {
        "match_id": str(manifest["match"]["id"]),
        "home": manifest["home"],
        "away": manifest["away"],
        "kickoff": manifest["match"]["opening_time"],
    }
    actual_match = {
        "match_id": str(evidence_match.get("match_id")),
        "home": evidence_match.get("home"),
        "away": evidence_match.get("away"),
        "kickoff": evidence_match.get("kickoff"),
    }
    if actual_match != expected_match:
        raise ValueError("manifest match does not match evidence JSON")
    provider = json.loads(provider_path.read_text(encoding="utf-8"))
    if provider.get("schema_version") != PROVIDER_SNAPSHOT_SCHEMA_VERSION:
        raise ValueError("unsupported provider snapshot schema")
    markets = [
        {"id": item["market_id"], "question": item["question"]}
        for item in evidence_json.get("question_evidence", [])
    ]
    intents = {
        item["market_id"]: item["intent"]
        for item in evidence_json.get("question_evidence", [])
        if item.get("intent") is not None
    }
    specs = {
        item["market_id"]: item.get("direct_market_spec")
        for item in evidence_json.get("question_evidence", [])
    }
    result = MatchResult(
        sp_match=manifest["match"], fixture=manifest.get("fixture"),
        home=manifest.get("home"), away=manifest.get("away"), markets=markets,
        intents=intents, intent_sources=manifest.get("intent_sources") or {},
        intent_resolutions=manifest.get("intent_resolutions") or {},
        market_specs=specs, af_books=provider.get("af_books") or [],
        oa_observations=provider.get("oa_observations") or [],
        evidence_json=evidence_json, evidence_path=str(evidence_path),
        evidence_hash=manifest["evidence_hash"], session_id=manifest["session_id"],
        session_manifest_path=str(manifest_path),
    )
    return result, evidence_json, evidence_path


def _result_from_legacy_session(
    session: dict,
) -> tuple[MatchResult, dict, Path]:
    evidence_path = Path(_container_path(session["evidence_path"]))
    evidence_json = json.loads(evidence_path.read_text(encoding="utf-8"))
    embedded_hash = evidence_json.get("evidence_hash")
    computed_hash = evidence.evidence_hash(evidence_json)
    if not embedded_hash or embedded_hash != computed_hash:
        raise ValueError("legacy evidence JSON content does not match its evidence_hash")
    if session.get("evidence_hash") != embedded_hash:
        raise ValueError("legacy session evidence_hash does not match evidence JSON")
    evidence_match = evidence_json.get("match") or {}
    expected_match = {
        "match_id": str(session["match"]["id"]),
        "home": session.get("home"),
        "away": session.get("away"),
        "kickoff": session["match"]["opening_time"],
    }
    actual_match = {
        "match_id": str(evidence_match.get("match_id")),
        "home": evidence_match.get("home"),
        "away": evidence_match.get("away"),
        "kickoff": evidence_match.get("kickoff"),
    }
    if actual_match != expected_match:
        raise ValueError("legacy session match does not match evidence JSON")
    evidence_markets = [
        {"id": item.get("market_id"), "question": item.get("question")}
        for item in evidence_json.get("question_evidence", [])
    ]
    if session.get("markets") != evidence_markets:
        raise ValueError("legacy session markets do not match evidence JSON")
    result = MatchResult(
        sp_match=session["match"], fixture=session.get("fixture"),
        home=session.get("home"), away=session.get("away"),
        markets=session.get("markets") or [], intents=session.get("intents") or {},
        market_specs=session.get("market_specs") or {},
        skip_reasons=session.get("skip_reasons") or {},
        af_books=session.get("af_books") or [],
        oa_observations=session.get("oa_observations") or [],
        evidence_json=evidence_json, evidence_path=str(evidence_path),
        evidence_hash=session.get("evidence_hash"),
        session_manifest_path=None,
    )
    return result, evidence_json, evidence_path


def _select_match(
    *, next_match: bool, query: str | None,
) -> tuple[SportPredict, dict, dict, dict, datetime]:
    sp = SportPredict()
    event = sp.event()
    lobby = sp.lobby(event["id"])
    now = datetime.now(timezone.utc)
    upcoming = sorted(
        (
            (match, _parse_kickoff(match["opening_time"]))
            for match in sp.matches(event["id"], lobby["id"])
            if _parse_kickoff(match["opening_time"]) > now
        ),
        key=lambda item: item[1],
    )
    if not upcoming:
        raise SystemExit("no upcoming open matches")
    if next_match:
        match, kickoff = upcoming[0]
        return sp, event, lobby, match, kickoff

    needle = str(query or "").casefold()
    exact = [item for item in upcoming if str(item[0]["id"]).casefold() == needle]
    matches = exact or [
        item for item in upcoming
        if needle in str(item[0].get("name") or "").casefold()
    ]
    if not matches:
        raise SystemExit(f"no upcoming match matches {query!r}")
    if len(matches) > 1:
        choices = ", ".join(
            f"{match.get('name', match['id'])} ({match['id']})"
            for match, _kickoff in matches
        )
        raise SystemExit(f"ambiguous match {query!r}; candidates: {choices}")
    match, kickoff = matches[0]
    return sp, event, lobby, match, kickoff


def _new_run_directory(match: dict) -> tuple[Path, str]:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    short_id = str(match["id"]).split("-", 1)[0]
    session_id = (
        f"{stamp}_{_slug(match.get('name') or str(match['id']))}_{short_id}_"
        f"{uuid.uuid4().hex[:8]}"
    )
    path = RUNS_DIR / session_id
    path.mkdir(parents=True, exist_ok=False)
    return path, session_id


def _artifact_ref(path: Path) -> dict:
    return {"path": str(path), "sha256": _sha256(path)}


def _verify_artifact(reference, label: str) -> Path:
    if not isinstance(reference, dict) or set(reference) != {"path", "sha256"}:
        raise ValueError(f"manifest artifact {label} is invalid")
    path = Path(_container_path(reference["path"]))
    if not path.is_file():
        raise ValueError(f"manifest artifact {label} is missing: {path}")
    if _sha256(path) != reference["sha256"]:
        raise ValueError(f"manifest artifact {label} failed hash verification")
    return path


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _object_hash(value: dict) -> str:
    payload = dict(value)
    payload.pop("manifest_hash", None)
    blob = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _verify_manifest_hash(manifest: dict) -> None:
    if manifest.get("manifest_hash") != _object_hash(manifest):
        raise ValueError("manifest content does not match its manifest_hash")


def _write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _intent_task(request_path: Path, response_path: Path, request: dict) -> str:
    return (
        "# Codex intent resolution\n\n"
        "Resolve only the unfamiliar SportPredict questions in the request. Do not "
        "price them. Return one canonical intent for every unresolved market using "
        "the enums and complete field set enforced by `bot/intent_resolution.py`. "
        "Include a two-leg `compound` only when required.\n\n"
        f"- Request: `{_host_path(request_path)}`\n"
        f"- Response: `{_host_path(response_path)}`\n"
        f"- Request ID: `{request['request_id']}`\n"
        f"- Response schema: `{intent_resolution.RESPONSE_SCHEMA_VERSION}`\n"
    )


def _pricing_task(
    *, session_id: str, evidence_hash: str, manifest_path: Path,
    evidence_path: Path, prompt_path: Path, response_path: Path,
    lineups_available: bool,
) -> str:
    lineup_note = (
        "Confirmed starting XIs are present in the evidence."
        if lineups_available else
        "Confirmed XIs are absent; research team news and disclose lineup uncertainty."
    )
    return (
        "# Manual SportPredict Codex pricing task\n\n"
        "Use Codex agent/subagent tools for pre-kickoff research and judgement. "
        "The repository never calls a model API. Read the pricing prompt and evidence, "
        "then write only the required JSON response.\n\n"
        f"- Session ID: `{session_id}`\n"
        f"- Evidence hash: `{evidence_hash}`\n"
        f"- Manifest: `{_host_path(manifest_path)}`\n"
        f"- Evidence: `{_host_path(evidence_path)}`\n"
        f"- Prompt: `{_host_path(prompt_path)}`\n"
        f"- Response: `{_host_path(response_path)}`\n\n"
        f"{lineup_note}\n"
    )


def _latest_submitted_payload(match_id: str, lobby_id: str) -> dict | None:
    if not ledger.LEDGER_PATH.exists():
        return None
    with ledger.connect() as db:
        db.row_factory = sqlite3.Row
        run = db.execute(
            """SELECT id, evidence_path FROM runs
               WHERE match_id = ? AND lobby_id = ? AND status = 'submitted'
               ORDER BY submitted_at DESC, recorded_at DESC LIMIT 1""",
            (match_id, lobby_id),
        ).fetchone()
        if not run:
            return None
        questions = db.execute(
            """SELECT market_id, probability_int FROM questions
               WHERE run_id = ? AND probability_int IS NOT NULL""",
            (run["id"],),
        ).fetchall()
    return {
        "run_id": run["id"],
        "evidence_path": run["evidence_path"],
        "payload": [
            {"market_id": row["market_id"], "lobby_id": lobby_id,
             "probability": row["probability_int"]}
            for row in questions
        ],
    }


def _submitted_session_run(session_id: str) -> str | None:
    """Return a successful ledger run for a prepared session, if one exists."""
    if not ledger.LEDGER_PATH.exists():
        return None
    with ledger.connect() as db:
        row = db.execute(
            """SELECT id FROM runs
               WHERE session_id = ? AND status = 'submitted'
               ORDER BY submitted_at DESC, recorded_at DESC LIMIT 1""",
            (session_id,),
        ).fetchone()
    return row["id"] if row else None


def _confirmed_lineups(lineups: list[dict] | None) -> bool:
    summary = evidence.summarize_lineups(lineups)
    return bool(summary and len(summary) >= 2) and all(
        len((team or {}).get("starting_xi") or []) >= 11
        for team in summary.values()
    )


def _print_predictions(result: MatchResult) -> None:
    rows = [
        {"market_id": prediction.market_id,
         "probability_int": prediction.probability_int,
         "question": prediction.question,
         "reasoning_summary": prediction.codex_reasoning_summary}
        for prediction in result.predictions
    ]
    print("PREDICTIONS_JSON=" + json.dumps(rows, ensure_ascii=False, sort_keys=True))
    for row in rows:
        suffix = f" — {row['reasoning_summary']}" if row["reasoning_summary"] else ""
        print(f"- {row['probability_int']:>2}%  {row['market_id']}  {row['question']}{suffix}")


def _print_match(match: dict, kickoff: datetime, minutes: float) -> None:
    print(f"MATCH={match.get('name', match['id'])}")
    print(f"MATCH_ID={match['id']}")
    print(f"KICKOFF={kickoff.isoformat()}")
    print(f"MINUTES_TO_KICKOFF={minutes:.1f}")


def _print_path(label: str, path: Path) -> None:
    print(f"{label}_PATH={_host_path(path)}")
    print(f"{label}_CONTAINER_PATH={_container_path(path)}")


def _minutes_before(kickoff: datetime) -> float:
    return (kickoff - datetime.now(timezone.utc)).total_seconds() / 60.0


def _parse_kickoff(opening_time: str) -> datetime:
    value = datetime.fromisoformat(opening_time.replace("Z", "+00:00"))
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _host_path(pathish) -> str:
    path = Path(str(pathish))
    host_root = os.environ.get("SPLLM_HOST_ROOT")
    if not host_root:
        return str(path)
    try:
        relative = path.relative_to(Path(__file__).resolve().parent.parent)
    except ValueError:
        return str(path)
    return str(Path(host_root) / relative)


def _container_path(pathish) -> str:
    path = Path(str(pathish))
    host_root = os.environ.get("SPLLM_HOST_ROOT")
    root = Path(__file__).resolve().parent.parent
    if host_root:
        try:
            return str(root / path.relative_to(Path(host_root)))
        except ValueError:
            pass
    return str(path)


def _slug(value: str) -> str:
    import re

    return re.sub(r"[^A-Za-z0-9]+", "_", value).strip("_").lower()[:80] or "match"


if __name__ == "__main__":
    main()
