#!/usr/bin/env bash
# Container entrypoint for the two deployed workflows. Prediction preparation
# and submission are explicit Codex/manual actions; cron invokes settlement
# only. Secrets are supplied at `docker run` time by cache/deployed/run.sh.
set -uo pipefail

case "${1:-}" in
  manual)
    shift
    exec python -m scripts.manual_submit "$@"
    ;;
  settle)
    shift
    exec python -m scripts.settle_ledger "$@"
    ;;
  *)
    echo "usage: entrypoint.sh {manual|settle} ..." >&2
    exit 64
    ;;
esac
