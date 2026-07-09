"""Cross-process lock shared by manual prediction and settlement workflows."""

from __future__ import annotations

import fcntl
from contextlib import contextmanager

from . import config


# Keep the historical path so a previously deployed immutable image coordinates
# with the refactored runner during rollout.
LOCK_PATH = config.ROOT / "cache" / "cron_submit.lock"


@contextmanager
def operation_lock():
    """Acquire the shared lock without waiting, or fail with an operator message."""
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOCK_PATH.open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("another prediction or settlement run is in progress") from exc
        yield
