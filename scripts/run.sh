#!/usr/bin/env bash
# Cron entrypoint: run one tick of the autonomous submitter inside the immutable
# v1 image. Because the code is baked into the image, the live bot is a frozen
# snapshot — editing the working tree never affects a running tick until the next
# scripts/deploy.sh rebuild. Installed into cron by scripts/deploy.sh; you
# normally never call this by hand.
#
#   scripts/run.sh [--status|--dry-run]
set -uo pipefail
export PATH="/usr/local/bin:/usr/bin:/bin:${PATH:-}"

# A cron shell may not have the `docker` group active yet (added via usermod
# without re-login). If the daemon isn't reachable, re-exec once under
# `sg docker` so the group applies.
if ! docker info >/dev/null 2>&1 && [ -z "${_RR_SG:-}" ] && command -v sg >/dev/null 2>&1; then
  export _RR_SG=1
  exec sg docker -c "$(printf '%q ' "$0" "$@")"
fi

cd "$(dirname "$0")/.."
ROOT="$PWD"
IMAGE="${SPLLM_IMAGE:-sportspredict-llm}"
TAG="${SPLLM_TAG:-v1}"

# Load this repo's .env and forward only the keys the bot needs — by reference,
# so secret values never appear in argv / `ps`.
if [ ! -f "$ROOT/.env" ]; then
  echo "FATAL: no $ROOT/.env (need SPORTSPREDICT_KEY etc.)" >&2
  exit 1
fi
set -a; . "$ROOT/.env"; set +a
: "${SPORTSPREDICT_KEY:?SPORTSPREDICT_KEY not set in .env}"

# Mount cache/ (paid odds cache, parser cache, cron markers + flock) and logs/
# (ledger, audit) so state persists across the per-tick --rm containers. --user
# keeps written files owned by the host user, not root.
mkdir -p "$ROOT/cache" "$ROOT/logs"
exec docker run --rm --user "$(id -u):$(id -g)" -e HOME=/tmp \
  -e SPORTSPREDICT_KEY -e APIFOOTBALL_KEY -e ODDS_API_KEY -e OPENAI_API_KEY \
  -e PARSER_MODEL -e ODDS_REGIONS -e LLM_PRICING_ENABLED -e LLM_PRICING_MODEL \
  -e SPORTSPREDICT_SIMULATOR_N_SIMS \
  -v "$ROOT/cache:/app/cache" \
  -v "$ROOT/logs:/app/logs" \
  "$IMAGE:$TAG" "$@"
