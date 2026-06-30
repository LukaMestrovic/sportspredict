# sportspredict-llm

A self-contained bot for the SportPredict × Jump Trading Probability Cup
(FIFA World Cup 2026). It converts bookmaker odds and match context into one
auditable evidence bundle, makes one web-grounded LLM pricing call per match,
and submits integer YES probabilities from 1–99.

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
4. `simulator/` contains the learned-rate simulator source, configuration, and
   fitted artifacts. `bot/simulator.py` invokes it through a JSON child-process
   boundary so numerical dependencies never leak into the lightweight bot.
5. `bot/llm_pricing.py` makes one cached web-grounded call and requires a
   complete per-market audit. Incomplete markets are skipped.
6. `bot/pipeline.py` records every submission through the SQLite ledger before
   upserting it to SportPredict.

Provider events are matched by kickoff and both teams; kickoff alone is not
unique when multiple group matches start together.

## Repository boundary

This repository contains the complete production bot. Runtime and deployment
do not read `../sportspredict`, `../sportspredict-hybrid`, a generated source
snapshot, or a prebuilt project wheel. The bundled simulator is intentionally
runtime-only: training pipelines, large historical corpora, notebooks, and
standalone competition clients are not copied into the production component.

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
| `LLM_PRICING_MODEL` | `gpt-5.4-mini` | final per-match pricing |
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

## Deployment

Prerequisites are Docker, a running Docker daemon, `crontab`, and a completed
`.env` in this checkout.

```bash
scripts/deploy.sh
scripts/run.sh --status
crontab -l
tail -f logs/cron.log
```

`scripts/deploy.sh` performs the whole deployment:

1. builds `sportspredict-llm:v1` from `docker/Dockerfile`;
2. smoke-tests the bundled learned model and its audit artifacts without keys;
3. runs a read-only SportPredict status check with keys passed at runtime; and
4. idempotently installs one per-minute cron block.

The dispatcher is normally a fast no-op. At T−30 it refreshes provider odds
once, fetches current lineups, forces a fresh cached pricing/web-search call for
that submission window, and refreshes exact WC2026 empirical rates from every
final API-Football fixture strictly before the target kickoff. Final event
responses and the compact tournament snapshot live in bind-mounted `cache/`, so
this stays current across short-lived containers without rebuilding the frozen
image after every match. It then submits through the ledger and writes its
audit. A file lock prevents overlapping ticks and a per-match marker prevents
duplicate fires.

The image is immutable between deploys. Re-run `scripts/deploy.sh` to ship new
code. `scripts/run.sh` bind-mounts this checkout's `cache/` and `logs/`, so paid
responses, parser/pricing caches, cron markers, evidence, audits, and the ledger
survive image rebuilds. Never delete those directories during deployment.

## Evidence and pricing contract

For every match, the evidence file contains:

- raw provider odds and per-book de-vigged probabilities for exact contracts;
- an explicit `contract_scope`: regulation is distinct from a full knockout
  match that can include extra time;
- one simulator fallback for supported questions without an exact direct quote
  (never a broad related-odds bundle), or explicit empty evidence when neither
  an exact quote nor a defensible simulator counter exists;
- lineups, injuries, team/referee history, venue, weather, and match metadata;
- `sportspredict-simulator` reports with stable contract keys, disclosed
  conditioning inputs, and historical performance; and
- provenance and freshness timestamps.

The pricing model must return `probability_int`, odds used, independent online
odds, non-odds factors, downweighted evidence, sources, and a concise reasoning
summary for every submitted market. The prompt is
`prompts/llm_pricing_prompt.md`; its hash is part of the cache key. Pricing
refuses to run after kickoff, and repeat manual runs reuse the frozen pre-match
audit.

## Ledger and settlement

`logs/prediction_ledger.sqlite3` records real questions, provider snapshots,
intent and market mapping, evidence/audit paths and hashes, submitted values,
errors, and eventual outcomes. All user-facing and scheduled submissions use
`pipeline.submit_with_ledger`.

Settlement is idempotent and joins by SportPredict `market_id`. It accepts only
the platform's explicit `current_value` of 0 or 100; it never infers results from
scores, web search, or Brier values.

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
- Final API-Football event timelines are fetched once and retained permanently;
  each T−30 fire rebuilds `cache/wc2026_empirical.json` with a strict target-time
  cutoff and separate all-stage/knockout coverage counts.
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
| LLM pricing | one cached multi-market call with web research |

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
analysis/, notebooks/    optional post-mortem tools; never deployed
cache/                   retained runtime cache; git-ignored
logs/                    retained evidence, audits, and ledger; git-ignored
run.py                   manual predict/submit CLI
validate.py              deterministic settled-fixture validation
```
