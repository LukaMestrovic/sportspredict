#!/usr/bin/env bash
# Cron wrapper: run the autonomous submitter from the repo root with the venv
# Python so `bot` and `.env` resolve regardless of cron's cwd/PATH.
set -euo pipefail
cd "$(dirname "$0")/.."
exec .venv/bin/python -m scripts.cron_submit "$@"
