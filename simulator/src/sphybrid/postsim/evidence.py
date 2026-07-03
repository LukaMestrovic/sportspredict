"""Read the compact historical evidence table used by simulator reports."""

from __future__ import annotations

import copy
import json
from pathlib import Path


class HistoricalEvidence:
    def __init__(
        self,
        records: dict[str, dict] | None = None,
        families: dict[str, dict] | None = None,
    ):
        self.records = records or {}
        self.families = families or {}

    @classmethod
    def load(cls, path: str | Path | None) -> "HistoricalEvidence":
        if not path or not Path(path).exists():
            return cls()
        try:
            raw = json.loads(Path(path).read_text())
            return cls(raw.get("contracts") or {}, raw.get("families") or {})
        except Exception:
            return cls()

    def get(self, key: str, family: str | None = None) -> dict:
        found = self.records.get(key)
        if found:
            result = copy.deepcopy(found)
        else:
            missing = {"available": False, "reason": "No exact historical label set for this contract."}
            result = {
                "contract_key": key,
                "model_performance": {"all_history": missing, "wc2026": missing},
                "empirical_rate": {"all_history": missing, "wc2026": missing},
            }
        if family:
            result["family_performance"] = copy.deepcopy(
                self.families.get(family)
                or {
                    "family": family,
                    "all_history": {
                        "available": False,
                        "reason": "No comparable unseen rows for this simulator family.",
                    },
                    "wc2026": {
                        "available": False,
                        "reason": "No comparable unseen WC2026 rows for this simulator family.",
                    },
                }
            )
        return result
