#!/usr/bin/env bash
# Container entrypoint: run settlement/status utilities, or the deployed manual
# Codex bridge. Arguments pass straight through, so `docker run <img> --status`,
# `--dry-run`, `--settle`, and `manual ...` work. The bot API key MUST be
# supplied as $SPORTSPREDICT_KEY (passed via -e by scripts/run.sh).
set -uo pipefail
: "${SPORTSPREDICT_KEY:?SPORTSPREDICT_KEY must be provided via -e}"
if [ "${1:-}" = "manual" ]; then
  shift
  exec python -m scripts.manual_submit "$@"
fi
exec python -m scripts.cron_submit "$@"
