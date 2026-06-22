#!/usr/bin/env bash
# One-command deploy. Builds the immutable v1 image from the CURRENT source and
# installs the per-minute cron schedule that runs it. After this the bot is
# autonomous AND isolated: editing the working tree never affects a running tick
# until you re-run this script. Re-running is safe and idempotent — it rebuilds
# the image and rewrites only the sportspredict-llm cron block.
#
#   scripts/deploy.sh
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT="$PWD"
IMAGE="${SPLLM_IMAGE:-sportspredict-llm}"
TAG="${SPLLM_TAG:-v1}"

# 1) Require the live keys.
if [ ! -f .env ]; then
  echo "FATAL: no .env — set SPORTSPREDICT_KEY, APIFOOTBALL_KEY, ODDS_API_KEY, OPENAI_API_KEY." >&2
  exit 1
fi

# 2) Build the immutable image (source baked in; secrets never baked).
echo ">> docker build $IMAGE:$TAG ..."
docker build -f docker/Dockerfile -t "$IMAGE:$TAG" .

# 3) Smoke-test the image without submitting: it must reach SportPredict and
#    report the next match. Keys are read from .env by reference, never argv.
echo ">> smoke-test image (--status, no submit) ..."
set -a; . ./.env; set +a
docker run --rm --user "$(id -u):$(id -g)" -e HOME=/tmp \
  -e SPORTSPREDICT_KEY -e APIFOOTBALL_KEY -e ODDS_API_KEY -e OPENAI_API_KEY \
  -v "$ROOT/cache:/app/cache" -v "$ROOT/logs:/app/logs" \
  "$IMAGE:$TAG" --status

# 4) Install the cron schedule (idempotent: replace any sportspredict-llm block).
echo ">> installing cron schedule ..."
begin="# >>> sportspredict-llm v1 >>>"
end="# <<< sportspredict-llm v1 <<<"
block="$(cat <<EOF
$begin
# Every minute: submit the next match's predictions at the 30-min and 5-min marks.
# Runs the immutable $IMAGE:$TAG image, so working-tree edits never affect a live
# tick; it is a fast no-op until a match is within 30 minutes of kickoff.
* * * * * $ROOT/scripts/run.sh >> $ROOT/logs/cron.log 2>&1
$end
EOF
)"
# Strip ANY existing versioned sportspredict-llm block so a tag bump never leaves two running.
( crontab -l 2>/dev/null | sed '/# >>> sportspredict-llm v.* >>>/,/# <<< sportspredict-llm v.* <<</d'; echo "$block" ) | crontab -

echo ">> deployed: image $IMAGE:$TAG, cron installed (every minute)."
echo "   logs:   tail -f $ROOT/logs/cron.log"
echo "   check:  crontab -l"
echo "   manual: scripts/run.sh --status   (or --dry-run)"
