"""Persistent on-disk JSON cache.

The Odds API is a **paid, metered** provider, so every event-odds response is
cached to disk and reused — a given (event, markets, regions) request hits the
network at most once per TTL window. Clear with `rm -rf cache/` or
`python -c "from bot.cache import clear; clear()"`.
"""
from __future__ import annotations

import hashlib
import json
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
    if (not refresh and p.exists()
            and (ttl <= 0 or time.time() - p.stat().st_mtime < ttl)):
        return json.loads(p.read_text())["value"]
    value = fetch()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"key": key, "fetched": time.time(), "value": value}))
    return value


def clear() -> None:
    import shutil
    if CACHE_DIR.exists():
        shutil.rmtree(CACHE_DIR)
