"""Persistent on-disk JSON cache.

The Odds API is a **paid, metered** provider, so every event-odds response is
cached to disk and reused — a given (event, markets, regions) request hits the
network at most once per TTL window. Runtime state is retained across deploys.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Callable

from . import config

CACHE_DIR = config.ROOT / "cache"
DEFAULT_TTL = 12 * 3600  # seconds; pre-match odds are stable enough for re-runs


def _path(namespace: str, key: str) -> Path:
    digest = hashlib.sha1(key.encode()).hexdigest()[:16]
    return CACHE_DIR / namespace / f"{digest}.json"


def get_or_fetch(
    namespace: str,
    key: str,
    fetch: Callable[[], Any],
    ttl: int = DEFAULT_TTL,
    *,
    refresh: bool = False,
) -> Any:
    """Return cached value for `key`, or fetch and store a current value.

    ``refresh`` bypasses an existing entry but still replaces it, so later
    callers reuse the newly fetched value rather than triggering another call.
    """
    p = _path(namespace, key)
    if not refresh:
        try:
            if p.exists() and (
                ttl <= 0 or time.time() - p.stat().st_mtime < ttl
            ):
                cached = json.loads(p.read_text(encoding="utf-8"))
                return cached["value"]
        except (OSError, ValueError, KeyError, TypeError):
            # A killed legacy writer may have left a partial entry. Refetch and
            # atomically replace it instead of failing the production run.
            pass
    value = fetch()
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{p.name}.", suffix=".tmp", dir=p.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump({"key": key, "fetched": time.time(), "value": value}, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, p)
    finally:
        temporary.unlink(missing_ok=True)
    return value
