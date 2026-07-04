# sportspredict-llm

A self-contained bot for the SportPredict × Jump Trading Probability Cup
(FIFA World Cup 2026). It converts bookmaker odds and match context into one
auditable evidence bundle, makes one staged web-grounded LLM pricing call per
match, and submits integer YES probabilities from 1–99.

## Architecture

```text
SportPredict questions
        │
        ▼
deterministic parser ──▶ provider market mapping
        │                        │
        └──────────────┬─────────┘
                       ▼
                 evidence JSON
          ┌────────────┼────────────┐
          │            │            │
   bookmaker odds  match context  bundled simulator
          └────────────┼────────────┘
                       ▼
          one cached web-grounded LLM call
          ├─ base prices from evidence
          ├─ match-read research markdown
          └─ per-question language adjustments
                       ▼
            audited 1–99 submissions + ledger
```

The main boundaries are:

1. `bot/parser.py` parses recurring templates deterministically. Unfamiliar
   wording is handled in at most one cached fallback call per match.
2. `bot/matcher.py`, `bot/predictor.py`, and `bot/oddsapi.py` map provider
   contracts and de-vig coherent outcomes from the same bookmaker.
3. `bot/evidence.py` emits exact odds when available, otherwise a bundled
   simulator fallback when that exact contract is supported, plus structured
   match context in one JSON file. Unsupported goal-method props are left
   explicitly empty for audited web research rather than matched approximately.
   Each market also carries a stable `Qn` label, a starting-price
   `decision_basis`, and a `subagent_brief` so the prompt-only LLM workflow can
   split match-read and question-specific research cleanly.
4. `simulator/` contains the learned-rate simulator source, lean training
   pipeline, compact training tables, configuration, and fitted artifacts.
   `bot/simulator.py` invokes it through a JSON child-process boundary so
   numerical dependencies never leak into the lightweight bot.
5. `bot/llm_pricing.py` makes one cached web-grounded call that first computes
   base prices, writes a markdown match read, then applies bounded language
   adjustments. Incomplete markets or invalid moves are skipped.
6. `bot/pipeline.py` records every submission through the SQLite ledger before
   upserting it to SportPredict.

Provider events are matched by kickoff and both teams; kickoff alone is not
unique when multiple group matches start together.

## Repository boundary

This repository contains the complete production bot: the scheduled entrypoints,
deterministic parsers, provider adapters, market matching logic, evidence
builder, web-grounded pricing layer, submission ledger, deployment files, and
the complete simulator used as labeled context. Runtime and deployment use only
source, configuration, compact benchmark exports, compact simulator training
tables, and fitted artifacts tracked in this checkout.

The bundled simulator is complete enough to retrain and validate in place, but
it is not a research dump. It includes the maintained training, ingestion,
backtest, event-timing, player-share, and evidence-building commands needed to
work on the simulator. It excludes exploratory notebooks, raw provider caches,
large historical archives, generated review outputs, and standalone competition
clients that are not used by this bot. That keeps production auditable while
still leaving the simulator improvable from this repository alone.

Analysis files that remain in `analysis/` are either small reproducibility tools
for the tracked simulator artifacts or current planning notes for WC2026 market
coverage. Generated runtime state stays in `cache/` and `logs/`, which are
retained on the machine but excluded from source control and Docker images.

The Docker build uses only files tracked here. `cache/`, `logs/`, `.env`, and
the local virtual environment are excluded from the image.

## Setup

Python 3.11+ is supported; deployment currently uses Python 3.14.

```bash
uv venv --python 3.14
uv pip install --python .venv/bin/python \
  -r requirements.txt -r simulator/requirements.txt
cp .env.example .env
```

The bot itself uses only the standard library plus `requests`. Numerical
packages are listed separately in `simulator/requirements.txt`.

Required `.env` keys:

| Key | Purpose |
|---|---|
| `SPORTSPREDICT_KEY` | SportPredict bot API |
| `APIFOOTBALL_KEY` | API-Football fixtures, lineups, statistics, and odds |
| `ODDS_API_KEY` | The Odds API; paid/metered and always cached |
| `OPENAI_API_KEY` | parser fallback and audited LLM pricing |

Useful optional settings:

| Key | Default | Purpose |
|---|---:|---|
| `PARSER_MODEL` | `gpt-5.4-mini` | unfamiliar question parsing |
| `LLM_PRICING_MODEL` | `gpt-5.5` | final per-match pricing |
| `LLM_PRICING_REASONING_EFFORT` | `high` | API fallback reasoning effort |
| `LLM_PRICING_SEARCH_CONTEXT_SIZE` | `medium` | Responses web-search context size |
| `LLM_PRICING_ENABLED` | `1` | set `0` for deterministic local checks |
| `ODDS_REGIONS` | `eu,uk,us` | Odds API breadth and credit use |
| `SPORTSPREDICT_SIMULATOR_N_SIMS` | `8000` | simulator draws; capped at 10000 |

## Usage

```bash
# Predict open matches without submitting
.venv/bin/python run.py predict

# Cheap end-to-end smoke check
.venv/bin/python run.py predict --limit 1

# Deterministic preview without web-grounded pricing
.venv/bin/python run.py predict --limit 1 --no-llm

# Manual submission; always recorded in the ledger
.venv/bin/python run.py predict --submit

# Settle new ledger rows from explicit SportPredict outcomes
.venv/bin/python -m scripts.settle_ledger
```

The live manual path is the deployed Codex workflow, not an OpenAI API pricing
call. Around T−75, run the deployed prepare command and require confirmed
lineups:

```bash
cache/deployed/run.sh manual status --next
cache/deployed/run.sh manual prepare --next --fresh --require-lineups
```

If `LINEUPS_AVAILABLE=true`, prepare writes the T−30 cron marker immediately
after confirmed XIs are detected and refreshes that marker with the generated
session/evidence paths before returning. That marker is the handoff that tells
cron a lineup-backed Codex submission owns the match. If
`LINEUPS_AVAILABLE=false`, prepare does not write the cron marker; do not submit
as a lineup-backed manual run.

After Codex writes the required JSON response to `RESPONSE_PATH`, submit through
the deployed runner:

```bash
cache/deployed/run.sh manual submit --session SESSION_PATH --response RESPONSE_PATH
```

For lineup-backed sessions, submit refreshes the same marker before reading or
validating the response, so a long manual submit started around T−45 cannot race
the T−30 automated OpenAI cron path. Manual sessions without confirmed lineups
do not create the cron marker automatically.

## Deployment

Prerequisites are Docker, a running Docker daemon, `crontab`, and a completed
`.env` in this checkout.

```bash
scripts/deploy.sh
cache/deployed/run.sh --status
crontab -l
tail -f logs/cron.log
```

`scripts/deploy.sh` performs the whole deployment:

1. builds an immutable `sportspredict-llm:<git-sha>-<timestamp>` image
   plus the convenience `sportspredict-llm:v1` alias from `docker/Dockerfile`;
2. smoke-tests the bundled learned model and its audit artifacts without keys;
3. runs a read-only SportPredict status check with keys passed at runtime; and
4. writes `cache/deployed/run.sh` pinned to that immutable image and
   idempotently installs the per-minute T−30 dispatcher and five-minute
   settlement/benchmark refresh cron entries through that runner.

The dispatcher is normally a fast no-op. At T−30 it first checks the per-match
cron marker. A lineup-backed manual Codex prepare/submit writes that marker as
soon as the manual flow is underway, before SportPredict verification, so cron
does not start a competing automated OpenAI submission. Cron's own markers also
block repeat fires. A manual run without confirmed lineups does not create the
marker automatically, and a no-lineups manual ledger row alone does not suppress
the T−30 lineup-backed cron refresh.

When no blocking marker or lineup-backed submitted ledger row exists, cron
refreshes provider odds once, fetches current lineups when available, forces a
fresh cached pricing/web-search call for that submission window, and refreshes
exact WC2026 empirical rates from every labelable final API-Football fixture
strictly before the target kickoff. Team contracts contribute two observations
per match where appropriate. Final event/stat/player responses and the compact
tournament snapshot live in bind-mounted `cache/`, so this stays current across
short-lived containers without rebuilding the frozen image after every match. It
then submits through the ledger and writes its audit. A file lock prevents
overlapping ticks.

A second cron tick runs settlement every five minutes. It accepts only explicit
SportPredict `current_value` outcomes, refreshes the exact-contract tournament
rates, and extends a simulator-only WC2026 benchmark. The benchmark starts from
a tracked 73-match replay and prices each newly settled match once with the
unchanged pre-2026 simulator artifacts; no LLM probabilities or reasoning enter
it. The T−30 tick refreshes this retained snapshot again before pricing, so each
new LLM evidence bundle sees every result settled so far.

The active image is immutable between deploys. Re-run `scripts/deploy.sh` to
ship new code. `cache/deployed/run.sh` bind-mounts this checkout's `cache/` and
`logs/`, so paid responses, parser/pricing caches, cron markers, evidence, audits, and the ledger
survive image rebuilds. Never delete those directories during deployment.

## Evidence and pricing contract

For every match, the evidence file contains:

- raw provider odds and per-book de-vigged probabilities for exact contracts;
- regulation first-team-to-score odds as the one explicit full-match proxy,
  labeled as such because the extra-time-only difference is accepted as immaterial;
- an explicit `contract_scope`: regulation is distinct from a full knockout
  match that can include extra time;
- one simulator fallback for supported questions without an exact direct quote
  (never a broad related-odds bundle), or explicit empty evidence when neither
  an exact quote nor a defensible simulator counter exists;
- lineups, injuries, team/referee history, venue, weather, and match metadata;
- `sportspredict-simulator` reports with stable contract keys, disclosed
  conditioning inputs, exact-contract empirical rates, and family-level Brier
  comparisons against always-50% and leakage-safe empirical-rate baselines; and
- provenance and freshness timestamps.

The LLM receives a compact simulator projection rather than the full internal
report: one percentage, its basis and adjustment directions, available empirical
rates as `(rate_pct, n)`, and one family-comparison row per useful scope. Model
provenance, repeated refresh metadata, unavailable scopes, derived deltas,
confidence intervals, and legacy performance blocks remain in the retained
artifacts/snapshots but are not repeated in every question sent to the LLM.

The pricing model must return top-level `match_read_markdown` and
`match_read_sources`, then for every submitted market a `base_probability_int`,
final `probability_int`, `language_adjustment`, odds used, independent online
odds, non-odds factors, downweighted evidence, sources, and a concise reasoning
summary. Movement from the base is validator-capped by evidence type, and
invalid moves are skipped rather than corrected silently. The prompt is
`prompts/llm_pricing_prompt.md`; its hash is part of the cache key. Pricing
refuses to run after kickoff, and repeat manual runs reuse the frozen pre-match
audit.

## Market catalog

The raw provider-market reference is retained as
`soccer_live_odds_market_catalog.pdf`. Use it when auditing or extending market
matching, and keep the deterministic mappings in `bot/matcher.py` aligned with
the exact provider contracts. Do not force unsupported, compound,
simulator-only, or full-match knockout questions onto approximate bookmaker
contracts.

## Ledger and settlement

`logs/prediction_ledger.sqlite3` records real questions, provider snapshots,
intent and market mapping, evidence/audit paths and hashes, submitted values,
errors, and eventual outcomes. All user-facing and scheduled submissions use
`pipeline.submit_with_ledger`.

Settlement is idempotent and joins by SportPredict `market_id`. It accepts only
the platform's explicit `current_value` of 0 or 100; it never infers results from
scores, web search, or Brier values.

Each settlement also refreshes `cache/simulator_family_benchmark.json`.
`all_history` uses rolling-origin predictions whose model and exact-contract
empirical-rate baseline were fitted before each test fold. `wc2026` is one
tournament-wide, simulator-only replay: model artifacts and empirical baselines
were frozen before 2026, while newly settled tournament questions are appended
automatically. It is never scoped to the current team or player. Every scope
reports unique-match sample size, match-clustered uncertainty, and an explicit
small-sample warning.

```bash
# One match, selected by id or name substring
.venv/bin/python -m scripts.settle_ledger --match "Portugal"

# All newly completed matches plus aggregate Brier reports
.venv/bin/python -m scripts.settle_ledger
```

## Caching and quota

- Every provider fetch goes through `bot/cache.py`.
- The Odds API cache key includes event, market, and regions because billing is
  `markets × regions`.
- API-Football fixtures and odds have TTLs; settled statistics are permanent.
- Final API-Football event, team-stat and player-stat responses are fetched once
  and retained permanently; settlement and T−30 fires rebuild
  `cache/wc2026_empirical.json` with a strict target-time cutoff and separate
  all-stage/knockout coverage counts.
- Settled frozen predictions rebuild `cache/simulator_family_benchmark.json`
  every five minutes and immediately before each T−30 evidence handoff.
- The T−30 job deliberately refreshes odds once, with identical requests
  deduplicated inside the run.
- Parser and compound fallback calls are cached by prompt version, model, and
  messages. The first pricing call is web-grounded and non-deterministic, but
  its result is cached permanently for repeatability.

Pre-match odds may disappear from providers a few days after kickoff. Retained
local cache entries are therefore part of the audit record, not disposable
build output.

## Validation

```bash
# Offline/unit coverage, including the real bundled simulator process
OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 MKL_NUM_THREADS=1 \
  .venv/bin/python -m unittest discover -s tests -p 'test_*.py'

# Settled-fixture deterministic validation; LLM pricing is disabled
.venv/bin/python validate.py --days 7

# Cheap live smoke using cached provider data where available
.venv/bin/python run.py predict --limit 1
```

`validate.py` uses final API-Football statistics and does not run web research,
which prevents settled-result leakage. Older fixtures may have fewer priceable
markets after providers purge their pre-match odds.

## Cost

SportPredict is free and API-Football is a flat-rate subscription. The metered
parts are:

| Source | Per-match behavior |
|---|---|
| Parser fallback | known templates cost $0; unfamiliar wording is one cached batch |
| Compound fallback | local for known forms; otherwise one cached batch |
| Odds API | requested markets × configured regions; one deliberate T−30 refresh |
| LLM pricing | one cached multi-market call with match-read research and adjustment audit |

Changing parser behavior can change per-match spend and must be reflected here.

## Repository layout

```text
bot/                    lightweight live bot and provider clients
simulator/
  src/                  baseline + learned runtime source
  config/               deterministic model/contract configuration
  data/                 fitted artifacts, lookup tables, Elo, audit evidence
  requirements.txt      numerical dependencies only
prompts/                 audited pricing prompt
scripts/
  cron_submit.py         T−30 dispatcher
  deploy.sh              build, smoke-test, and install cron
  run.sh                 cron-safe container runner
  settle_ledger.py       explicit-outcome settlement and Brier reporting
tests/                   unit and bundled-runtime integration tests
analysis/                compact benchmark regeneration tools and exports
soccer_live_odds_market_catalog.pdf
                         raw provider market reference
cache/                   retained runtime cache; git-ignored
logs/                    retained evidence, audits, and ledger; git-ignored
run.py                   manual predict/submit CLI
validate.py              deterministic settled-fixture validation
```

The tracked family benchmark artifact is regenerated from compact rolling-origin
exports under `analysis/data/simulator_benchmarks/`:

```bash
.venv/bin/python analysis/build_simulator_family_benchmarks.py
```
