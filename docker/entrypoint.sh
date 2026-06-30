#!/usr/bin/env bash
# Container entrypoint: run one tick of the autonomous submitter. The dispatcher
# decides whether to fire (the 30-/5-min marks before the next kickoff) and exits
# fast otherwise. Arguments pass straight through, so `docker run <img> --status`
# `docker run <img> --dry-run`, and `docker run <img> --settle` work. The bot API key MUST be supplied as
# $SPORTSPREDICT_KEY (passed via -e by scripts/run.sh).
set -uo pipefail
: "${SPORTSPREDICT_KEY:?SPORTSPREDICT_KEY must be provided via -e}"
exec python -m scripts.cron_submit "$@"
