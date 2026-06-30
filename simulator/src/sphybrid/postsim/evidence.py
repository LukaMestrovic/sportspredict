"""Read the compact historical evidence table used by the LLM-facing report."""

from __future__ import annotations

import json
from pathlib import Path


class HistoricalEvidence:
    def __init__(self, records: dict[str, dict] | None = None):
        self.records = records or {}

    @classmethod
    def load(cls, path: str | Path | None) -> "HistoricalEvidence":
        if not path or not Path(path).exists():
            return cls()
        try:
            raw = json.loads(Path(path).read_text())
            return cls(raw.get("contracts") or {})
        except Exception:
            return cls()

    def get(self, key: str) -> dict:
        found = self.records.get(key)
        if found:
            return found
        missing = {"available": False, "reason": "No exact historical label set for this contract."}
        return {
            "contract_key": key,
            "model_performance": {"all_history": missing, "wc2026": missing},
            "empirical_rate": {"all_history": missing, "wc2026": missing},
        }
