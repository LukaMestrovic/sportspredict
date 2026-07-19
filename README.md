# sportspredict-llm

A production workflow for pricing FIFA World Cup 2026 questions in the
SportPredict Probability Cup. The repository builds deterministic bookmaker and
simulator evidence; a manually operated Codex agent with subagents researches
the match and supplies the final audited probabilities.

The application does not call a language-model API. There is no OpenAI client,
model configuration, or `OPENAI_API_KEY`. Prediction preparation and submission
are explicit operator actions. Only settlement and benchmark refresh are
scheduled.

## Production flow

```text
SportPredict match + questions
              │
              ▼
 deterministic parser ── unfamiliar wording ──► Codex intent response
              │                                  │
              └──────── versioned local registry ◄┘
              │
              ▼
 catalogue-first exact provider contracts + per-book de-vigging
              │
              ├── exact API-Football / Odds API contract
              ├── exact cached public-web candidate
              ├── disclosed live-odds proxy + simulator blend
              └── simulator-only / researched fallback
              │
              ▼
 immutable run directory: evidence + prompt + manifest hashes
              │
              ▼
 manual Codex agent/subagent research and audited JSON response
              │
              ▼
 strict local validation ─► SQLite ledger ─► SportPredict ─► verification
```

The application owns deterministic extraction, provider mapping, evidence,
validation, submission, and settlement. Codex owns pre-kickoff research and the
judgement required to convert that evidence into final 1–99 YES probabilities.

## What is retained

This is deliberately a production repository, not an analysis archive. It
contains:

- provider clients, deterministic parsing and market mapping;
- exact-contract de-vigging and the evidence builder;
- the manual Codex response boundary and audit renderer;
- the production simulator runtime, configuration, compact lookups, and fitted
  artifacts;
- the prediction ledger, explicit-outcome settlement, and live WC2026 benchmark
  refresh;
- immutable Docker deployment and tests.

Offline ingestion, training, exploratory analysis, generic prediction CLIs,
automated prediction cron jobs, and paid model-API paths are intentionally not
present. Retraining the bundled simulator happens outside this repository.

`cache/` and `logs/` are retained machine state. They are ignored by Git and
excluded from the image, but bind-mounted into deployed containers. Never delete
them during cleanup or deployment: they hold metered provider responses, intent
resolutions, manifests, audits, benchmark state, and the ledger.

Historical `logs/llm_pricing_runs/`, legacy session JSON, and the ledger's old
`llm_*` SQLite columns remain readable for audit compatibility. New work is
written under `logs/codex_runs/` and uses Codex naming.

## Setup

Deployment uses Python 3.14. Create a local environment and install the bot and
simulator dependency sets separately:

```bash
uv venv --python 3.14
uv pip install --python .venv/bin/python \
  -r requirements.txt -r simulator/requirements.txt
cp .env.example .env
```

Required `.env` values:

| Key | Purpose |
|---|---|
| `SPORTSPREDICT_KEY` | SportPredict discovery, predictions, and results |
| `APIFOOTBALL_KEY` | fixtures, lineups, statistics, injuries, and odds |
| `ODDS_API_KEY` | exact secondary odds; quota-metered and cached; free plans supported |

Optional settings:

| Key | Default | Purpose |
|---|---:|---|
| `ODDS_REGIONS` | `eu,uk,us` | Odds API coverage and credit usage |
| `SPORTSPREDICT_SIMULATOR_N_SIMS` | `8000` | simulator draws, capped at 10000 |
| `REFEREE_SCAN_LEAGUES` | configured in `bot/config.py` | referee-history competitions |
| `REFEREE_SCAN_SEASONS` | previous two seasons | referee-history seasons |

## Manual prediction workflow

Use the deployed runner for production. It is pinned to an immutable image; do
not run a changing working tree against the live competition.

### 1. Inspect the match

Around 80 minutes before kickoff:

```bash
cache/deployed/run.sh manual status --next
# or select one exact ID / unique name fragment
cache/deployed/run.sh manual status --match "France vs Morocco"
```

Status reports kickoff, lineup availability, the latest submitted ledger run,
and platform verification when a prior submission exists. `--fresh` refreshes
the lineup sources without submitting anything.

### 2. Prepare one immutable handoff

```bash
cache/deployed/run.sh manual prepare --next --fresh
# or
cache/deployed/run.sh manual prepare --match MATCH_ID --fresh
```

`--fresh` deliberately refreshes provider state once. Confirmed starting XIs
are preferred, but missing or failed lineup lookups are warning-only. The
evidence records that uncertainty so Codex can research and disclose it.

Known questions are parsed locally. If every question is known, preparation
prints `STATUS=prepared` and paths for:

- `manifest.json` — run identity, versions, provider/evidence metadata, and
  SHA-256 references;
- `evidence.json` — deterministic pricing evidence;
- `provider_snapshot.json` — raw retained provider observations;
- `prompt.md` — the exact run-local Codex instructions;
- `task.md` — the paths, session ID, evidence hash, and lineup warning;
- `response.json` — where the Codex result must be written.

The prompt is copied into the run directory. Later working-tree edits therefore
cannot make Codex read a different prompt from the one submission verifies.

### 3. Resolve unfamiliar wording when requested

An unfamiliar question stops preparation before any metered Odds API request and
prints `STATUS=needs_intents`. Read `intent_task.md` and
`intent_request.json`, have Codex write the strict canonical response to
`intent_response.json`, then run the printed resume command:

```bash
cache/deployed/run.sh manual resume \
  --request logs/codex_runs/SESSION/intent_request.json \
  --intents logs/codex_runs/SESSION/intent_response.json \
  --fresh
```

Resume rechecks the live match, kickoff, full question set, fixture, and teams
before installing anything. Accepted answers are stored immutably under
`cache/intent_resolutions/v1/`, keyed by parser version, exact question, and
teams. Seeing the same wording later is deterministic. Conflicting answers fail
closed. A recurring contract should eventually be promoted into a tracked rule
in `bot/parser.py` with tests; runtime entries remain provenance for old runs.

Intent resolution is semantic only; a response cannot inject provider bet IDs,
regular expressions, or executable fallback logic. For every unfamiliar
contract, inspect `soccer_live_odds_market_catalog.pdf` first and promote an
exact pre-match mapping only when period, subject, threshold, and settlement all
match. Live/in-play bet IDs use a separate namespace and are not pre-match
direct evidence. If no coherent exact catalogue contract is present, the
deterministic order is exact cached public-web odds, then an allowlisted
related-market proxy mixed with the exact-contract simulator, then a disclosed
one-source or researched fallback. Near matches and overlapping selections
never enter `direct_odds`.

### 4. Run Codex research and pricing

Give Codex the generated `task.md`. It should read the run-local prompt and
evidence, use agent/subagent research passes, and write only the specified JSON
to `response.json`.

The response is bound to the exact response schema, session ID, and evidence
hash. It must contain every evidence market exactly once, all match-read aspect
memos, base-pricing and question-adjustment memos, non-empty public sources,
integer probabilities, and complete market audits. The validator rejects:

- missing, duplicate, extra, stale, or cross-session markets;
- booleans, fractional/non-finite values, or probabilities outside 1–99;
- unexplained movement, invalid confidence, or movement beyond evidence-specific
  caps;
- silent omission of supplied direct odds or pre-collected online candidates;
- replacing a supplied proxy/simulator blend with an unaudited base, or omitting
  one of its retained proxy observations;
- incomplete public reasoning or sources.

### 5. Submit and verify once

```bash
cache/deployed/run.sh manual submit \
  --session logs/codex_runs/SESSION/manifest.json \
  --response logs/codex_runs/SESSION/response.json
```

Submission refuses to run after kickoff. It verifies the manifest, every
artifact hash, evidence's internal hash, match binding, parser/evidence/response
versions, and Codex response before touching SportPredict. It records the actual
submission window, writes the ledger first, upserts integer 1–99 values, reads
the platform back, and marks the run submitted only if every intended open
market matches. A successfully submitted session cannot be replayed; prepare a
new session for a deliberate revision.

All user-facing submission paths must go through
`bot.pipeline.submit_with_ledger`.

## Deterministic parser and market contracts

`bot/parser.py` maps recurring Probability Cup templates without a network or
model call. `bot/intent_resolution.py` owns the strict offline request/response
contract for unfamiliar wording and the append-only runtime registry.

`bot/matcher.py` maps canonical intents to provider contracts.
`bot/predictor.py` and `bot/oddsapi.py` retain one de-vigged observation per
bookmaker rather than hiding dispersion behind an average. De-vigging is only
performed over a coherent outcome set from the same bookmaker and contract.
Compound evidence uses separately priced components and an explicit correlation
assumption; a marginal line is never presented as an exact compound price.

`bot/live_odds_proxy.py` owns the small allowlist of related-contract recipes.
It uses only already-fetched pre-match API-Football observations. When a target
has no exact price, it compares each live proxy component with the same
component from the simulator, transfers a capped residual on the log-odds
scale, and mixes that residual with the exact-target simulator baseline. The
evidence retains the recipe, weight, companion prices, every bookmaker, and the
resulting range; it labels the result non-direct throughout.

Provider matches are linked by kickoff and both teams, including when only one
fixture happens to share the kickoff. A team mismatch fails closed.

## Evidence contract

`bot/evidence.py` emits one versioned JSON bundle per prepared match. It includes:

- exact provider-contract observations with bookmaker, raw odds, conversion,
  and de-vig method;
- explicit settlement scope, especially regulation versus a knockout match that
  can include extra time;
- pre-collected public odds candidates for unsupported provider contracts;
- an allowlisted non-direct live-odds proxy and deterministic proxy/simulator
  blend when exact provider and public-web prices are absent and both inputs
  exist;
- deterministic compound components when available;
- lineups, injuries, team/player form, referee, venue, and provider-error
  provenance;
- one compact learned-simulator estimate where an exact quote is absent and the
  contract is supported;
- exact-contract empirical rates and leakage-safe family Brier comparisons;
- stable question IDs, decision-basis instructions, and Codex subagent briefs.

Simulator estimates and proxy recipes are labeled and retained, never hidden
anchors. Unsupported questions remain explicit research tasks. Exact direct
odds have priority. When a blend is primary, the response validator requires
the rounded blend as the audited base and requires every retained proxy book to
be used or explicitly downweighted. The sole override is a newly researched
exact online price with a retained URL, quote, conversion, direct-use label, and
an integer base inside its price spread; that path receives the tighter online
movement cap.

## Ledger and settlement

`logs/prediction_ledger.sqlite3` records the real questions, intent provenance,
provider snapshots, evidence and manifest paths/hashes, final audits,
submission window, platform status, and explicit outcomes.

Settlement joins by SportPredict `market_id` and accepts only the platform's
explicit `current_value` of 0 or 100. It never infers results from scores, search,
or Brier values.

```bash
# Local read-only review of one retained run
.venv/bin/python -m scripts.settle_ledger --match "France"

# Settle newly explicit outcomes and refresh empirical/simulator benchmarks
.venv/bin/python -m scripts.settle_ledger
```

Prediction and settlement share a cross-process lock. The settlement refresh
also updates `cache/wc2026_empirical.json` and
`cache/simulator_family_benchmark.json` using frozen simulator predictions and
target-time-safe evidence.

## Caching and cost

Provider JSON is written atomically through `bot/cache.py`. Partial legacy cache
entries are refetched. Manual `--fresh` refreshes once per client, while
identical requests remain deduplicated in memory.

- SportPredict has no metered pricing call in this repository.
- API-Football is rate-limited; its responses are cached by endpoint contract.
- The Odds API is quota-metered (including its free plan) and bills credits by
  requested markets × regions.
  The event/market/region cache is retained across deployments.
- Codex work is manual through the user's Codex environment. This repository
  makes zero paid OpenAI API calls.

Only a known 422 “market bundle unavailable” response becomes empty Odds API
evidence. Authentication, quota, rate, network, and server failures are
sanitized so query-string secrets cannot appear in errors, then propagated
instead of being cached as “no odds.”

## Deployment

Prerequisites: Docker, a running Docker daemon, `crontab`, and the three provider
keys in `.env`.

```bash
scripts/deploy.sh
cache/deployed/run.sh manual status --next
crontab -l
```

Deployment refuses a dirty working tree so the recorded Git commit exactly
matches the image source. It then:

1. builds `sportspredict-llm:<commit>-<UTC timestamp>` plus the `v1` alias;
2. smoke-tests the real bundled learned simulator and fitted artifacts without
   secrets;
3. runs a read-only live `manual status --next` container smoke test;
4. atomically publishes `cache/deployed/run.sh` and `current.json`, pinned to the
   immutable tag;
5. idempotently installs only the five-minute `settle` cron block.

The generated runner passes only the three provider keys at runtime. No secrets
are baked into the image or placed in command arguments. Re-run
`scripts/deploy.sh` to ship a new commit; changing the working tree never changes
the active image.

## Validation

```bash
OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 MKL_NUM_THREADS=1 \
  .venv/bin/python -m unittest discover -s tests -p 'test_*.py'

bash -n docker/entrypoint.sh scripts/deploy.sh
git diff --check
```

`scripts/deploy.sh` additionally performs the containerized simulator and live
status smoke tests. A production prepare is not a deployment smoke test because
`--fresh` may consume metered odds credits.

## Repository layout

```text
bot/                         deterministic production application
  parser.py                  tracked question templates
  intent_resolution.py       offline Codex intent handoff and registry
  matcher.py                 exact provider-contract mappings
  predictor.py, oddsapi.py   per-book de-vigged observations
  evidence.py                versioned deterministic evidence bundle
  codex_pricing.py           local response validator and audit renderer
  pipeline.py                prepare and ledger-backed submission boundary
  ledger.py                  durable SQLite audit/settlement state
simulator/                   production numerical runtime and fitted artifacts
prompts/codex_pricing_prompt.md
scripts/codex_workflow.py    status/prepare/resume/submit CLI
scripts/settle_ledger.py     explicit outcome settlement and review
scripts/deploy.sh            immutable image deployment and settlement cron
docker/                      production image and restricted entrypoint
tests/                       unit and real simulator integration coverage
soccer_live_odds_market_catalog.pdf
                             retained raw provider-market reference
cache/                       retained runtime state, ignored
logs/                        retained evidence/audits/ledger, ignored
```
