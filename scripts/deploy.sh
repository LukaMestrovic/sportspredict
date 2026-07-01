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
COMMIT="$(git rev-parse --short HEAD 2>/dev/null || echo unknown)"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
TAG="${SPLLM_TAG:-${COMMIT}-${STAMP}}"
ALIAS_TAG="${SPLLM_ALIAS_TAG:-v1}"

# 1) Require the live keys.
if [ ! -f .env ]; then
  echo "FATAL: no .env — set SPORTSPREDICT_KEY, APIFOOTBALL_KEY, ODDS_API_KEY, OPENAI_API_KEY." >&2
  exit 1
fi

# 2) Build the immutable image from this repository (secrets never baked).
#    --provenance=false keeps this a single image manifest: the default BuildKit
#    attestation manifest can fail to unpack on the containerd snapshotter
#    ("failed to prepare extraction snapshot ... parent snapshot does not exist")
#    even though the image is otherwise fine. We deploy one local arch, so the
#    attestation buys nothing here.
echo ">> docker build $IMAGE:$TAG ..."
docker build --provenance=false -f docker/Dockerfile \
  -t "$IMAGE:$TAG" -t "$IMAGE:$ALIAS_TAG" .

# 3) Smoke-test the bundled simulator without any secrets. This proves that
#    config + all fitted artifacts load: the goal/card
#    windows exercise event_timing.json, the any-player brace exercises
#    player_shares.json, and the brace's populated historical_evidence proves
#    simulation_evidence.json was baked and loaded. Missing artifacts would
#    degrade to neutral priors / unavailable evidence, so a returned non-degenerate
#    probability AND a contract_key AND live brace history are the real checks.
echo ">> smoke-test image simulator bridge ..."
docker run --rm --entrypoint python -e SPORTSPREDICT_SIMULATOR_N_SIMS=500 \
  "$IMAGE:$TAG" -c '
from bot.simulator import simulator_estimates
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
# Schema 2.1 projection: every estimate carries a stable contract key and family benchmark.
assert all(v.get("contract_key") for v in out.values()), out
# simulation_evidence.json baked + loaded: brace has a real all-history rate.
brace_hist = (out["brace"].get("historical_evidence") or {}).get("empirical_rate", {})
assert brace_hist.get("all_history", {}).get("available") is True, out["brace"]
brace_family = (out["brace"].get("historical_evidence") or {}).get("family_performance", {})
assert brace_family.get("all_history", {}).get("available") is True, out["brace"]
assert "empirical_rate" in brace_family["all_history"]["brier"], out["brace"]
print("simulator bridge OK:", {k: (v["family"], v["contract_key"], v["probability_pct"]) for k, v in sorted(out.items())})
'

# 4) Smoke-test the image without submitting: it must reach SportPredict and
#    report the next match. Keys are read from .env by reference, never argv.
echo ">> smoke-test image (--status, no submit) ..."
set -a; . ./.env; set +a
export LLM_PRICING_MODEL="${LLM_PRICING_MODEL:-gpt-5.5}"
export LLM_PRICING_REASONING_EFFORT="${LLM_PRICING_REASONING_EFFORT:-high}"
docker run --rm --user "$(id -u):$(id -g)" -e HOME=/tmp \
  -e SPORTSPREDICT_KEY -e APIFOOTBALL_KEY -e ODDS_API_KEY -e OPENAI_API_KEY \
  -e LLM_PRICING_MODEL -e LLM_PRICING_REASONING_EFFORT \
  -e SPLLM_HOST_ROOT="$ROOT" \
  -v "$ROOT/cache:/app/cache" -v "$ROOT/logs:/app/logs" \
  "$IMAGE:$TAG" --status

# 5) Write the active deployed runner. Cron and manual submissions use this
#    untracked script, pinned to the immutable image tag above.
echo ">> writing active deployed runner ..."
DEPLOYED_DIR="$ROOT/cache/deployed"
DEPLOYED_RUNNER="$DEPLOYED_DIR/run.sh"
mkdir -p "$DEPLOYED_DIR"
cat > "$DEPLOYED_RUNNER" <<EOF
#!/usr/bin/env bash
set -uo pipefail
export PATH="/usr/local/bin:/usr/bin:/bin:\${PATH:-}"
ROOT="$ROOT"
IMAGE="$IMAGE"
TAG="$TAG"

if ! docker info >/dev/null 2>&1 && [ -z "\${_RR_SG:-}" ] && command -v sg >/dev/null 2>&1; then
  export _RR_SG=1
  exec sg docker -c "\$(printf '%q ' "\$0" "\$@")"
fi

cd "\$ROOT"
if [ ! -f "\$ROOT/.env" ]; then
  echo "FATAL: no \$ROOT/.env (need SPORTSPREDICT_KEY etc.)" >&2
  exit 1
fi
set -a; . "\$ROOT/.env"; set +a
: "\${SPORTSPREDICT_KEY:?SPORTSPREDICT_KEY not set in .env}"
export LLM_PRICING_MODEL="\${LLM_PRICING_MODEL:-gpt-5.5}"
export LLM_PRICING_REASONING_EFFORT="\${LLM_PRICING_REASONING_EFFORT:-high}"

mkdir -p "\$ROOT/cache" "\$ROOT/logs"
exec docker run --rm --user "\$(id -u):\$(id -g)" -e HOME=/tmp \\
  -e SPORTSPREDICT_KEY -e APIFOOTBALL_KEY -e ODDS_API_KEY -e OPENAI_API_KEY \\
  -e PARSER_MODEL -e ODDS_REGIONS -e LLM_PRICING_ENABLED -e LLM_PRICING_MODEL \\
  -e LLM_PRICING_REASONING_EFFORT -e SPLLM_HOST_ROOT="\$ROOT" \\
  -e SPORTSPREDICT_SIMULATOR_N_SIMS \\
  -v "\$ROOT/cache:/app/cache" \\
  -v "\$ROOT/logs:/app/logs" \\
  "\$IMAGE:\$TAG" "\$@"
EOF
chmod +x "$DEPLOYED_RUNNER"
cat > "$DEPLOYED_DIR/current.json" <<EOF
{"image":"$IMAGE","tag":"$TAG","alias_tag":"$ALIAS_TAG","commit":"$COMMIT","deployed_at":"$STAMP","runner":"$DEPLOYED_RUNNER"}
EOF

# 6) Install the cron schedule (idempotent: replace any sportspredict-llm block).
echo ">> installing cron schedule ..."
begin="# >>> sportspredict-llm v1 >>>"
end="# <<< sportspredict-llm v1 <<<"
block="$(cat <<EOF
$begin
# Every minute: submit the next match's predictions at the 30-min mark.
# Runs the immutable $IMAGE:$TAG image, so working-tree edits never affect a live
# tick; it is a fast no-op until a match is within 30 minutes of kickoff.
* * * * * $DEPLOYED_RUNNER >> $ROOT/logs/cron.log 2>&1
# Every five minutes: settle explicit SportPredict outcomes, extend the
# tournament-wide frozen-simulator replay, and refresh WC2026 empirical rates.
2-59/5 * * * * $DEPLOYED_RUNNER --settle >> $ROOT/logs/settle.log 2>&1
$end
EOF
)"
# Strip ANY existing versioned sportspredict-llm block so a tag bump never leaves two running.
( crontab -l 2>/dev/null | sed '/# >>> sportspredict-llm v.* >>>/,/# <<< sportspredict-llm v.* <<</d'; echo "$block" ) | crontab -

echo ">> deployed: image $IMAGE:$TAG (alias $IMAGE:$ALIAS_TAG), cron installed (every minute)."
echo "   logs:   tail -f $ROOT/logs/cron.log"
echo "   check:  crontab -l"
echo "   manual: $DEPLOYED_RUNNER manual status --next"
