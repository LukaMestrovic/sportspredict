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
HYBRID_ROOT="${SPORTSPREDICT_HYBRID_ROOT:-$ROOT/../sportspredict-hybrid}"
HYBRID_SNAPSHOT="$ROOT/.deploy/hybrid_snapshot"

# 1) Require the live keys.
if [ ! -f .env ]; then
  echo "FATAL: no .env — set SPORTSPREDICT_KEY, APIFOOTBALL_KEY, ODDS_API_KEY, OPENAI_API_KEY." >&2
  exit 1
fi

# 2) Snapshot the sibling hybrid simulator so the deployed image is isolated
#    from later edits in BOTH working trees. Keep this explicit: no raw caches,
#    no secrets, only source/config/model artifacts needed by the bridge.
if [ ! -d "$HYBRID_ROOT/src/sphybrid" ]; then
  echo "FATAL: hybrid checkout not found at $HYBRID_ROOT (set SPORTSPREDICT_HYBRID_ROOT)." >&2
  exit 1
fi
for required in \
  "$HYBRID_ROOT/pyproject.toml" \
  "$HYBRID_ROOT/README.md" \
  "$HYBRID_ROOT/wheels/sportspredict-0.1.0-py3-none-any.whl" \
  "$HYBRID_ROOT/data/processed/rate_model.joblib" \
  "$HYBRID_ROOT/data/processed/rate_model.json" \
  "$HYBRID_ROOT/data/processed/team_ratings.parquet" \
  "$HYBRID_ROOT/data/processed/event_timing.json" \
  "$HYBRID_ROOT/data/processed/player_shares.parquet"
do
  if [ ! -f "$required" ]; then
    echo "FATAL: required hybrid deploy artifact missing: $required" >&2
    exit 1
  fi
done

echo ">> snapshot hybrid simulator from $HYBRID_ROOT ..."
rm -rf "$HYBRID_SNAPSHOT"
mkdir -p "$HYBRID_SNAPSHOT/data/processed" "$HYBRID_SNAPSHOT/data/raw"
cp -a "$HYBRID_ROOT/pyproject.toml" "$HYBRID_ROOT/README.md" "$HYBRID_SNAPSHOT/"
cp -a "$HYBRID_ROOT/src" "$HYBRID_ROOT/config" "$HYBRID_ROOT/wheels" "$HYBRID_SNAPSHOT/"
cp -a \
  "$HYBRID_ROOT/data/processed/rate_model.joblib" \
  "$HYBRID_ROOT/data/processed/rate_model.json" \
  "$HYBRID_ROOT/data/processed/team_ratings.parquet" \
  "$HYBRID_ROOT/data/processed/event_timing.json" \
  "$HYBRID_ROOT/data/processed/player_shares.parquet" \
  "$HYBRID_SNAPSHOT/data/processed/"
if [ -f "$HYBRID_ROOT/data/raw/elo.csv" ]; then
  cp -a "$HYBRID_ROOT/data/raw/elo.csv" "$HYBRID_SNAPSHOT/data/raw/"
fi

# 3) Build the immutable image (source baked in; secrets never baked).
#    --provenance=false keeps this a single image manifest: the default BuildKit
#    attestation manifest can fail to unpack on the containerd snapshotter
#    ("failed to prepare extraction snapshot ... parent snapshot does not exist")
#    even though the image is otherwise fine. We deploy one local arch, so the
#    attestation buys nothing here.
echo ">> docker build $IMAGE:$TAG ..."
docker build --provenance=false -f docker/Dockerfile -t "$IMAGE:$TAG" .

# 4) Smoke-test the baked hybrid bridge without any secrets. This proves the
#    deployed image uses its internal /sportspredict-hybrid snapshot (not either
#    live working tree) AND that config + both fitted artifacts load: the goal/
#    card windows exercise event_timing.json and the any-player brace exercises
#    player_shares.parquet. Missing artifacts would degrade to neutral priors, so
#    a returned, non-degenerate probability for each family is the real check.
echo ">> smoke-test image hybrid bridge ..."
docker run --rm --entrypoint python -e SPORTSPREDICT_HYBRID_N_SIMS=500 \
  "$IMAGE:$TAG" -c '
from bot.hybrid_model import simulator_estimates
from bot.pricing import PriceCtx
markets = [
    {"id": "pen", "question": "Will a penalty kick be awarded in the match?"},
    {"id": "goal", "question": "Will a goal be scored before the first hydration break?"},
    {"id": "card", "question": "Will a card be shown after the second hydration break?"},
    {"id": "brace", "question": "Will any player score more than 1 goal (excluding own goals) in the match?"},
]
ctx = PriceCtx("Argentina", "Austria", [], None, None)
# Empty lists => no direct price => every market is sent to the simulator, which
# resolves what it can. No parser/LLM (and so no secrets) needed for selection.
direct = {m["id"]: [] for m in markets}
out = simulator_estimates(markets, ctx, direct_by_market=direct,
                          kickoff="2026-06-22T17:00:00Z", stage="knockout")
assert set(out) == {"pen", "goal", "card", "brace"}, out
assert {v["model"]["rate_model"] for v in out.values()} == {"LearnedRateModel"}, out
assert all(0.0 < v["probability"] < 1.0 for v in out.values()), out
print("hybrid bridge OK:", {k: (v["family"], v["probability_pct"]) for k, v in sorted(out.items())})
'

# 5) Smoke-test the image without submitting: it must reach SportPredict and
#    report the next match. Keys are read from .env by reference, never argv.
echo ">> smoke-test image (--status, no submit) ..."
set -a; . ./.env; set +a
docker run --rm --user "$(id -u):$(id -g)" -e HOME=/tmp \
  -e SPORTSPREDICT_KEY -e APIFOOTBALL_KEY -e ODDS_API_KEY -e OPENAI_API_KEY \
  -v "$ROOT/cache:/app/cache" -v "$ROOT/logs:/app/logs" \
  "$IMAGE:$TAG" --status

# 6) Install the cron schedule (idempotent: replace any sportspredict-llm block).
echo ">> installing cron schedule ..."
begin="# >>> sportspredict-llm v1 >>>"
end="# <<< sportspredict-llm v1 <<<"
block="$(cat <<EOF
$begin
# Every minute: submit the next match's predictions at the 30-min mark.
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
