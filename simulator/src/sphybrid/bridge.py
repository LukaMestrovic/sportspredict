"""JSON stdin/stdout entry point for the bundled simulator runtime."""

from __future__ import annotations

import json
import sys

from sportspredict.config import default_settings

from .report import simulation_report_from_payload


def main() -> int:
    payload = json.load(sys.stdin)
    report = simulation_report_from_payload(payload, settings=default_settings())
    json.dump(report, sys.stdout, ensure_ascii=False, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
