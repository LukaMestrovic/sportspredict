# AGENTS.md — guidance for coding agents

## Purpose

This repository is the production SportPredict Probability Cup workflow for
FIFA World Cup 2026. It deterministically prepares provider/simulator evidence;
a manually operated Codex agent and subagents research and price the match. See
`README.md` for the operator flow and architecture.

## Non-negotiable architecture

- The application makes no language-model API call. Do not add an OpenAI SDK,
  model endpoint, model environment setting, or `OPENAI_API_KEY`.
- Prediction status, preparation, unfamiliar-intent resolution, and submission
  are explicit `scripts.codex_workflow` actions. Do not add a prediction cron or
  generic auto-predict entrypoint. Cron runs settlement only.
- Known questions and compound templates are parsed deterministically in
  `bot/parser.py`. Unfamiliar wording produces a strict offline request handled
  through `bot/intent_resolution.py`; its accepted result is stored in the
  versioned append-only runtime registry. Never add an uncached or hidden parser
  fallback.
- Codex prices only from the prepared evidence plus disclosed pre-kickoff
  research. `bot/codex_pricing.py` is a pure local JSON validator/audit renderer,
  not a model client.
- Submit integer probabilities from 1 through 99. Every user-facing path must
  call `pipeline.submit_with_ledger`, then verify the values from SportPredict.
- Settlement accepts only explicit SportPredict `current_value` outcomes. Never
  infer an outcome from a score, web search, or Brier value.

## Production boundaries

- Keep the lightweight bot to the standard library plus `requests`.
  `simulator/requirements.txt` is the separate numerical dependency set.
- `simulator/` is a production runtime containing source, config, compact
  lookups, and fitted artifacts. Training, ingestion, notebooks, and offline
  backtests belong elsewhere and must not be reintroduced.
- Provider mapping and conversion remain deterministic and auditable in
  `matcher.py`, `predictor.py`, `oddsapi.py`, and `evidence.py`.
- De-vig only coherent outcomes from the same bookmaker and exact contract.
  Retain per-book observations; do not hide disagreement behind a final average.
- Compose compounds from separately priced components with disclosed
  correlation. Never relabel a marginal market as an exact compound contract.
- Exact bookmaker evidence has priority. Simulator values are labeled context,
  not hidden anchors.
- Provider fixture/event matching must verify kickoff and both teams, even for
  a single kickoff candidate. Ambiguity or mismatch fails closed.

## Run integrity

- New runs live in `logs/codex_runs/<session>/` and contain a copied prompt,
  evidence, provider snapshot, task, manifest, response, and audit outputs.
- Preserve response-schema, parser-schema, evidence-schema, session, match, and
  artifact-hash checks. Submission must refuse stale/cross-run input and kickoff
  that has passed.
- A successfully submitted session is single-use. A deliberate update requires
  a newly prepared session.
- Missing lineups are warning-only, but their absence or provider error must be
  visible in evidence and the manifest.
- Preserve legacy ledger column names and validated schema-1 session reading for
  historical audit compatibility. New application code uses Codex naming.

## Quota, caching, and secrets

- The Odds API is quota-metered; API-Football is rate-limited. Every reusable
  response goes through `bot/cache.py`. Keep writes atomic and refreshes
  deduplicated.
- Odds API billing scales with requested markets × regions. Request only exact
  needed markets and keep the cache key bound to event, market set, and regions.
- `--fresh` deliberately refreshes once. Do not add repeated metered fetches inside
  market loops.
- Do not convert authentication, quota, rate, network, or server errors into
  empty evidence. Provider exceptions must not expose query-string keys.
- `.env` is ignored and must never be committed or printed. Required keys are
  only `SPORTSPREDICT_KEY`, `APIFOOTBALL_KEY`, and `ODDS_API_KEY`.
- Treat ignored `cache/` and `logs/` as durable production state. Never remove
  them during cleanup, testing, image builds, or deployment.

## Git and deployment

- Commit after every working increment. Keep commits focused and messages clear.
- Preserve unrelated user changes in a dirty worktree.
- Deploy only with `scripts/deploy.sh`; do not hand-edit crontab or run the
  working tree as production.
- Deployment requires a clean tree, builds an immutable image, smoke-tests it,
  atomically publishes the pinned runner, and installs only settlement cron.
- Manual production actions must use `cache/deployed/run.sh`.

## Validation before committing

```bash
OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 MKL_NUM_THREADS=1 \
  .venv/bin/python -m unittest discover -s tests -p 'test_*.py'
bash -n docker/entrypoint.sh scripts/deploy.sh
git diff --check
```

Deployment performs additional simulator and live status checks. Avoid a live
`manual prepare --fresh` merely as a smoke test because it can consume Odds API
credits.

## Useful constants

- API-Football World Cup: `league=1`, `season=2026`.
- The Odds API sport: `soccer_fifa_world_cup`.
- New audits: `logs/codex_runs/`; historical audit/log names remain readable.
- Ledger: `logs/prediction_ledger.sqlite3`.
