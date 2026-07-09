"""Offline hand-off contract for questions the deterministic parser cannot map.

The production parser never calls a language-model API.  Instead, unfamiliar
wording is described by a versioned request.  A Codex run can return canonical
intents in the matching response format, which is validated against the exact
match and question set before being installed in an append-only registry.

Registry entries are keyed by parser version, the two teams, and the exact
question text.  They are deliberately immutable: installing the same answer is
idempotent, while a different answer for an existing key is an error.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Mapping

from .matcher import MARKET_KEYS


PARSER_SCHEMA_VERSION = "deterministic-v1"
REQUEST_SCHEMA_VERSION = "intent-resolution-request-v1"
RESPONSE_SCHEMA_VERSION = "intent-resolution-response-v1"
REGISTRY_SCHEMA_VERSION = "intent-resolution-registry-v1"
DEFAULT_REGISTRY_DIR = (
    Path(__file__).resolve().parent.parent / "cache" / "intent_resolutions" / "v1"
)

_SUBJECTS = {"home", "away", "match", "player"}
_COMPARATORS = {
    "yes", "win", "gte", "lte", "eq", "odd", "even", "more",
    "second_half_more",
}
_PERIODS = {"match", "1H", "2H"}
_TIME_SCOPES = {"regulation", "full_match", "penalty_shootout"}
_COUNT_COMPARATORS = {"gte", "lte", "eq"}
_INTENT_FIELDS = {
    "market", "subject", "player", "comparator", "threshold", "period",
    "time_scope", "excludes_own_goals",
}


class IntentResolutionError(ValueError):
    """An intent request, response, or registry entry is invalid."""


class IntentResolutionConflictError(IntentResolutionError):
    """A registry key already exists with a different canonical answer."""


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _exact_question(question: Any) -> str:
    if not isinstance(question, str) or not question.strip():
        raise IntentResolutionError("question must be a non-empty string")
    return unicodedata.normalize("NFC", question.strip())


def _identifier(value: Any, label: str) -> str:
    if not isinstance(value, (str, int)) or isinstance(value, bool):
        raise IntentResolutionError(f"{label} must be a non-empty string or integer")
    result = str(value).strip()
    if not result:
        raise IntentResolutionError(f"{label} must be non-empty")
    return result


def _require_exact_keys(
    value: Any, required: set[str], label: str, *, optional: set[str] | None = None,
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise IntentResolutionError(f"{label} must be an object")
    optional = optional or set()
    keys = set(value)
    missing = required - keys
    extra = keys - required - optional
    if missing:
        raise IntentResolutionError(
            f"{label} is missing field(s): {', '.join(sorted(missing))}"
        )
    if extra:
        raise IntentResolutionError(
            f"{label} has unknown field(s): {', '.join(sorted(extra))}"
        )
    return value


def resolution_key(question: str, home: str, away: str) -> str:
    """Return the immutable registry key for one exact question contract."""
    exact = _exact_question(question)
    if not isinstance(home, str) or not home.strip():
        raise IntentResolutionError("home must be a non-empty string")
    if not isinstance(away, str) or not away.strip():
        raise IntentResolutionError("away must be a non-empty string")
    return _digest({
        "parser_schema_version": PARSER_SCHEMA_VERSION,
        "home": home.strip(),
        "away": away.strip(),
        "question": exact,
    })


def question_set_hash(questions: Iterable[Mapping[str, Any]]) -> str:
    """Hash a complete market/question snapshot independent of API ordering."""
    canonical: list[dict[str, str]] = []
    seen: set[str] = set()
    for index, item in enumerate(questions):
        if not isinstance(item, Mapping):
            raise IntentResolutionError(f"questions[{index}] must be an object")
        market_id = _identifier(item.get("id", item.get("market_id")), "market_id")
        if market_id in seen:
            raise IntentResolutionError(f"duplicate market_id: {market_id}")
        seen.add(market_id)
        canonical.append({
            "market_id": market_id,
            "question": _exact_question(item.get("question")),
        })
    return _digest(sorted(canonical, key=lambda item: item["market_id"]))


def _validate_kickoff(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise IntentResolutionError(f"{label} must be a non-empty ISO timestamp")
    result = value.strip()
    try:
        parsed = datetime.fromisoformat(result.replace("Z", "+00:00"))
    except ValueError as exc:
        raise IntentResolutionError(f"{label} must be an ISO timestamp") from exc
    if parsed.tzinfo is None:
        raise IntentResolutionError(f"{label} must include a timezone")
    return result


def validate_intent(intent: Any, *, label: str = "intent") -> dict[str, Any]:
    """Validate and return one complete canonical parser intent."""
    value = _require_exact_keys(intent, _INTENT_FIELDS, label)
    market = value["market"]
    if market not in MARKET_KEYS:
        raise IntentResolutionError(f"{label}.market is not supported: {market!r}")
    subject = value["subject"]
    if subject not in _SUBJECTS:
        raise IntentResolutionError(f"{label}.subject is invalid: {subject!r}")
    comparator = value["comparator"]
    if comparator not in _COMPARATORS:
        raise IntentResolutionError(
            f"{label}.comparator is invalid: {comparator!r}"
        )
    period = value["period"]
    if period not in _PERIODS:
        raise IntentResolutionError(f"{label}.period is invalid: {period!r}")
    time_scope = value["time_scope"]
    if time_scope not in _TIME_SCOPES:
        raise IntentResolutionError(
            f"{label}.time_scope is invalid: {time_scope!r}"
        )
    if not isinstance(value["excludes_own_goals"], bool):
        raise IntentResolutionError(f"{label}.excludes_own_goals must be boolean")

    player = value["player"]
    if subject == "player":
        if not isinstance(player, str) or not player.strip():
            raise IntentResolutionError(
                f"{label}.player must name the player for a player subject"
            )
        player = player.strip()
    elif player is not None:
        raise IntentResolutionError(
            f"{label}.player must be null for a non-player subject"
        )

    threshold = value["threshold"]
    if comparator in _COUNT_COMPARATORS:
        if (not isinstance(threshold, int) or isinstance(threshold, bool)
                or threshold < 0):
            raise IntentResolutionError(
                f"{label}.threshold must be a non-negative integer for {comparator}"
            )
    elif threshold is not None:
        raise IntentResolutionError(
            f"{label}.threshold must be null for comparator {comparator}"
        )

    return {
        "market": market,
        "subject": subject,
        "player": player,
        "comparator": comparator,
        "threshold": threshold,
        "period": period,
        "time_scope": time_scope,
        "excludes_own_goals": value["excludes_own_goals"],
    }


def validate_compound(compound: Any, *, label: str = "compound") -> dict[str, Any]:
    """Validate an optional two-leg deterministic decomposition."""
    value = _require_exact_keys(compound, {"op", "components"}, label)
    if value["op"] not in {"AND", "OR"}:
        raise IntentResolutionError(f"{label}.op must be AND or OR")
    components = value["components"]
    if not isinstance(components, list) or len(components) != 2:
        raise IntentResolutionError(f"{label}.components must contain exactly two legs")
    normalized = []
    for index, component in enumerate(components):
        component_label = f"{label}.components[{index}]"
        item = _require_exact_keys(component, {"question", "intent"}, component_label)
        normalized.append({
            "question": _exact_question(item["question"]),
            "intent": validate_intent(
                item["intent"], label=f"{component_label}.intent",
            ),
        })
    if normalized[0]["question"] == normalized[1]["question"]:
        raise IntentResolutionError(f"{label} components must be distinct")
    return {"op": value["op"], "components": normalized}


def build_resolution_request(
    *,
    match_id: str | int,
    kickoff: str,
    home: str,
    away: str,
    questions: Iterable[Mapping[str, Any]],
    unresolved: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    """Build a request bound to an exact match and complete question snapshot."""
    match_id_text = _identifier(match_id, "match_id")
    kickoff_text = _validate_kickoff(kickoff, "kickoff")
    if not isinstance(home, str) or not home.strip():
        raise IntentResolutionError("home must be a non-empty string")
    if not isinstance(away, str) or not away.strip():
        raise IntentResolutionError("away must be a non-empty string")

    canonical_questions: list[dict[str, str]] = []
    seen: set[str] = set()
    for index, item in enumerate(questions):
        if not isinstance(item, Mapping):
            raise IntentResolutionError(f"questions[{index}] must be an object")
        market_id = _identifier(item.get("id", item.get("market_id")), "market_id")
        if market_id in seen:
            raise IntentResolutionError(f"duplicate market_id: {market_id}")
        seen.add(market_id)
        canonical_questions.append({
            "market_id": market_id,
            "question": _exact_question(item.get("question")),
        })
    by_id = {item["market_id"]: item for item in canonical_questions}

    canonical_unresolved: list[dict[str, str]] = []
    unresolved_seen: set[str] = set()
    for index, item in enumerate(unresolved):
        if not isinstance(item, Mapping):
            raise IntentResolutionError(f"unresolved[{index}] must be an object")
        market_id = _identifier(
            item.get("market_id", item.get("id")), "unresolved market_id",
        )
        if market_id in unresolved_seen:
            raise IntentResolutionError(f"duplicate unresolved market_id: {market_id}")
        unresolved_seen.add(market_id)
        if market_id not in by_id:
            raise IntentResolutionError(
                f"unresolved market_id is absent from question set: {market_id}"
            )
        question = _exact_question(item.get("question"))
        if question != by_id[market_id]["question"]:
            raise IntentResolutionError(
                f"unresolved question does not match market_id {market_id}"
            )
        normalized = item.get("normalized_question")
        if not isinstance(normalized, str) or not normalized.strip():
            raise IntentResolutionError(
                f"unresolved[{index}].normalized_question must be non-empty"
            )
        canonical_unresolved.append({
            "market_id": market_id,
            "question": question,
            "normalized_question": normalized.strip(),
            "resolution_key": resolution_key(question, home, away),
            "reason": "unrecognized-question",
        })

    q_hash = _digest(sorted(canonical_questions, key=lambda item: item["market_id"]))
    body: dict[str, Any] = {
        "schema_version": REQUEST_SCHEMA_VERSION,
        "parser_schema_version": PARSER_SCHEMA_VERSION,
        "match": {
            "id": match_id_text,
            "home": home.strip(),
            "away": away.strip(),
            "kickoff": kickoff_text,
        },
        "question_set_hash": q_hash,
        "questions": canonical_questions,
        "unresolved": canonical_unresolved,
    }
    body["request_id"] = _digest(body)
    validate_resolution_request(body)
    return body


def validate_resolution_request(request: Any) -> dict[str, Any]:
    """Validate request structure and all of its integrity hashes."""
    value = _require_exact_keys(request, {
        "schema_version", "request_id", "parser_schema_version", "match",
        "question_set_hash", "questions", "unresolved",
    }, "request")
    if value["schema_version"] != REQUEST_SCHEMA_VERSION:
        raise IntentResolutionError("unsupported request schema_version")
    if value["parser_schema_version"] != PARSER_SCHEMA_VERSION:
        raise IntentResolutionError("request parser_schema_version is stale")
    match = _require_exact_keys(
        value["match"], {"id", "home", "away", "kickoff"}, "request.match",
    )
    match_id = _identifier(match["id"], "request.match.id")
    for key in ("home", "away"):
        if not isinstance(match[key], str) or not match[key].strip():
            raise IntentResolutionError(f"request.match.{key} must be non-empty")
    kickoff = _validate_kickoff(match["kickoff"], "request.match.kickoff")
    if not isinstance(value["questions"], list):
        raise IntentResolutionError("request.questions must be a list")
    questions: list[dict[str, str]] = []
    ids: set[str] = set()
    for index, raw in enumerate(value["questions"]):
        item = _require_exact_keys(
            raw, {"market_id", "question"}, f"request.questions[{index}]",
        )
        market_id = _identifier(item["market_id"], "market_id")
        if market_id in ids:
            raise IntentResolutionError(f"duplicate market_id: {market_id}")
        ids.add(market_id)
        questions.append({
            "market_id": market_id,
            "question": _exact_question(item["question"]),
        })
    expected_q_hash = _digest(sorted(questions, key=lambda item: item["market_id"]))
    if value["question_set_hash"] != expected_q_hash:
        raise IntentResolutionError("request question_set_hash does not match questions")
    if not isinstance(value["unresolved"], list) or not value["unresolved"]:
        raise IntentResolutionError("request.unresolved must be a non-empty list")
    by_id = {item["market_id"]: item for item in questions}
    unresolved_ids: set[str] = set()
    unresolved: list[dict[str, str]] = []
    for index, raw in enumerate(value["unresolved"]):
        label = f"request.unresolved[{index}]"
        item = _require_exact_keys(raw, {
            "market_id", "question", "normalized_question", "resolution_key",
            "reason",
        }, label)
        market_id = _identifier(item["market_id"], f"{label}.market_id")
        if market_id in unresolved_ids:
            raise IntentResolutionError(f"duplicate unresolved market_id: {market_id}")
        unresolved_ids.add(market_id)
        if market_id not in by_id:
            raise IntentResolutionError(
                f"unresolved market_id is absent from question set: {market_id}"
            )
        question = _exact_question(item["question"])
        if question != by_id[market_id]["question"]:
            raise IntentResolutionError(
                f"unresolved question does not match market_id {market_id}"
            )
        if not isinstance(item["normalized_question"], str) \
                or not item["normalized_question"].strip():
            raise IntentResolutionError(f"{label}.normalized_question must be non-empty")
        expected_key = resolution_key(question, match["home"], match["away"])
        if item["resolution_key"] != expected_key:
            raise IntentResolutionError(f"{label}.resolution_key does not match")
        if item["reason"] != "unrecognized-question":
            raise IntentResolutionError(f"{label}.reason is invalid")
        unresolved.append({
            "market_id": market_id,
            "question": question,
            "normalized_question": item["normalized_question"].strip(),
            "resolution_key": item["resolution_key"],
            "reason": item["reason"],
        })

    body = {
        "schema_version": value["schema_version"],
        "parser_schema_version": value["parser_schema_version"],
        "match": {
            "id": match_id,
            "home": match["home"],
            "away": match["away"],
            "kickoff": kickoff,
        },
        "question_set_hash": value["question_set_hash"],
        "questions": questions,
        "unresolved": unresolved,
    }
    if value["request_id"] != _digest(body):
        raise IntentResolutionError("request_id does not match request contents")
    return {**body, "request_id": value["request_id"]}


def validate_resolution_response(
    request: Any, response: Any,
) -> dict[str, Any]:
    """Validate a response against the exact request; fail closed on drift."""
    req = validate_resolution_request(request)
    value = _require_exact_keys(response, {
        "schema_version", "request_id", "parser_schema_version", "match_id",
        "question_set_hash", "resolutions",
    }, "response")
    if value["schema_version"] != RESPONSE_SCHEMA_VERSION:
        raise IntentResolutionError("unsupported response schema_version")
    if value["request_id"] != req["request_id"]:
        raise IntentResolutionError("response is for a different request_id")
    if value["parser_schema_version"] != PARSER_SCHEMA_VERSION:
        raise IntentResolutionError("response parser_schema_version is stale")
    if _identifier(value["match_id"], "response.match_id") != req["match"]["id"]:
        raise IntentResolutionError("response is for a different match_id")
    if value["question_set_hash"] != req["question_set_hash"]:
        raise IntentResolutionError("response is for a different question set")
    if not isinstance(value["resolutions"], list):
        raise IntentResolutionError("response.resolutions must be a list")

    requested = {item["market_id"]: item for item in req["unresolved"]}
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw in enumerate(value["resolutions"]):
        label = f"response.resolutions[{index}]"
        item = _require_exact_keys(
            raw, {"market_id", "question", "intent"}, label,
            optional={"compound"},
        )
        market_id = _identifier(item["market_id"], f"{label}.market_id")
        if market_id in seen:
            raise IntentResolutionError(f"duplicate resolution market_id: {market_id}")
        seen.add(market_id)
        if market_id not in requested:
            raise IntentResolutionError(f"unexpected resolution market_id: {market_id}")
        question = _exact_question(item["question"])
        if question != requested[market_id]["question"]:
            raise IntentResolutionError(
                f"response question does not match market_id {market_id}"
            )
        resolved = {
            "market_id": market_id,
            "question": question,
            "intent": validate_intent(item["intent"], label=f"{label}.intent"),
        }
        if "compound" in item:
            resolved["compound"] = validate_compound(
                item["compound"], label=f"{label}.compound",
            )
            if resolved["intent"]["market"] != "none":
                raise IntentResolutionError(
                    f"{label}.intent.market must be 'none' with a compound"
                )
        normalized.append(resolved)
    missing = set(requested) - seen
    if missing:
        raise IntentResolutionError(
            f"response is missing resolution(s): {', '.join(sorted(missing))}"
        )
    return {
        "schema_version": RESPONSE_SCHEMA_VERSION,
        "request_id": req["request_id"],
        "parser_schema_version": PARSER_SCHEMA_VERSION,
        "match_id": req["match"]["id"],
        "question_set_hash": req["question_set_hash"],
        "resolutions": normalized,
    }


def _registry_path(key: str, registry_dir: str | Path | None) -> Path:
    directory = Path(registry_dir) if registry_dir is not None else DEFAULT_REGISTRY_DIR
    return directory / f"{key}.json"


def _read_registry_entry(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise IntentResolutionError(f"cannot read registry entry {path}: {exc}") from exc
    item = _require_exact_keys(value, {
        "schema_version", "parser_schema_version", "resolution_key", "question",
        "home", "away", "intent", "compound", "provenance",
    }, "registry entry")
    if item["schema_version"] != REGISTRY_SCHEMA_VERSION:
        raise IntentResolutionError(f"unsupported registry schema in {path}")
    if item["parser_schema_version"] != PARSER_SCHEMA_VERSION:
        raise IntentResolutionError(f"stale parser schema in registry entry {path}")
    if not isinstance(item["resolution_key"], str) or not item["resolution_key"]:
        raise IntentResolutionError(f"invalid resolution key in registry entry {path}")
    _exact_question(item["question"])
    for team in ("home", "away"):
        if not isinstance(item[team], str) or not item[team].strip():
            raise IntentResolutionError(
                f"invalid {team} team in registry entry {path}"
            )
    validate_intent(item["intent"], label="registry entry.intent")
    if item["compound"] is not None:
        validate_compound(item["compound"], label="registry entry.compound")
    provenance = _require_exact_keys(item["provenance"], {
        "request_id", "match_id", "kickoff", "question_set_hash", "market_id",
    }, "registry entry.provenance")
    for key in ("request_id", "match_id", "question_set_hash", "market_id"):
        if not isinstance(provenance[key], str) or not provenance[key]:
            raise IntentResolutionError(
                f"invalid provenance {key} in registry entry {path}"
            )
    _validate_kickoff(provenance["kickoff"], "registry entry.provenance.kickoff")
    return dict(item)


def lookup_resolution(
    question: str,
    home: str,
    away: str,
    *,
    registry_dir: str | Path | None = None,
) -> dict[str, Any] | None:
    """Load a previously accepted exact-question resolution, if one exists."""
    key = resolution_key(question, home, away)
    path = _registry_path(key, registry_dir)
    if not path.exists():
        return None
    entry = _read_registry_entry(path)
    expected = {
        "resolution_key": key,
        "question": _exact_question(question),
        "home": home.strip(),
        "away": away.strip(),
    }
    for field, expected_value in expected.items():
        if entry[field] != expected_value:
            raise IntentResolutionError(
                f"registry entry {path} has mismatched {field}"
            )
    return {
        "intent": validate_intent(entry["intent"], label="registry entry.intent"),
        "compound": (
            validate_compound(entry["compound"], label="registry entry.compound")
            if entry["compound"] is not None else None
        ),
        "resolution_key": key,
        "registry_path": str(path),
        "provenance": dict(entry["provenance"]),
    }


def _same_answer(existing: Mapping[str, Any], candidate: Mapping[str, Any]) -> bool:
    fields = (
        "parser_schema_version", "resolution_key", "question", "home", "away",
        "intent", "compound",
    )
    return all(existing.get(field) == candidate.get(field) for field in fields)


def _atomic_create_json(path: Path, value: Mapping[str, Any]) -> bool:
    """Atomically create ``path``; return False if another writer won."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (_canonical_json(value) + "\n").encode("utf-8")
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temp_name, path)
        except FileExistsError:
            return False
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        return True
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


def install_resolution_response(
    request: Any,
    response: Any,
    *,
    registry_dir: str | Path | None = None,
) -> dict[str, dict[str, Any]]:
    """Validate and atomically install every response item.

    Returns the accepted answers by market ID, tagged as the current manual
    Codex resolution.  A later parser lookup tags the same immutable answer as
    ``runtime-resolution``.
    """
    req = validate_resolution_request(request)
    validated = validate_resolution_response(req, response)
    unresolved_by_id = {item["market_id"]: item for item in req["unresolved"]}
    candidates: list[tuple[dict[str, Any], Path, dict[str, Any]]] = []
    for item in validated["resolutions"]:
        market_id = item["market_id"]
        unresolved = unresolved_by_id[market_id]
        key = unresolved["resolution_key"]
        entry = {
            "schema_version": REGISTRY_SCHEMA_VERSION,
            "parser_schema_version": PARSER_SCHEMA_VERSION,
            "resolution_key": key,
            "question": item["question"],
            "home": req["match"]["home"],
            "away": req["match"]["away"],
            "intent": item["intent"],
            "compound": item.get("compound"),
            "provenance": {
                "request_id": req["request_id"],
                "match_id": req["match"]["id"],
                "kickoff": req["match"]["kickoff"],
                "question_set_hash": req["question_set_hash"],
                "market_id": market_id,
            },
        }
        path = _registry_path(key, registry_dir)
        candidates.append((item, path, entry))

    # Detect every existing conflict before creating any entry, so a bad
    # multi-question response cannot be partially installed.
    for _item, path, entry in candidates:
        if path.exists() and not _same_answer(_read_registry_entry(path), entry):
            raise IntentResolutionConflictError(
                f"resolution conflicts with immutable registry entry {path}"
            )

    accepted: dict[str, dict[str, Any]] = {}
    for item, path, entry in candidates:
        market_id = item["market_id"]
        key = entry["resolution_key"]
        created = _atomic_create_json(path, entry)
        if not created:
            existing = _read_registry_entry(path)
            if not _same_answer(existing, entry):
                raise IntentResolutionConflictError(
                    f"resolution conflicts with immutable registry entry {path}"
                )
        accepted[market_id] = {
            "intent": item["intent"],
            "intent_source": "manual-codex-resolution",
            "compound": item.get("compound"),
            "resolution_key": key,
            "registry_path": str(path),
        }
    return accepted
