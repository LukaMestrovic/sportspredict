# AGENTS.md — guidance for AI coding agents in this repo

## What this is
A minimal LLM bot that prices SportPredict Probability Cup questions for FIFA
WC2026 from bookmaker odds. See [README.md](README.md) for the architecture.

## Commit regularly
**Commit after every working increment** — a new market mapping, a bug fix, a
doc change. Keep commits small and focused with a clear message. Do not let
working changes pile up uncommitted. Never commit `.env` or any secret (it is
git-ignored; keep it that way).

## Conventions
- Pure standard library + `requests` for the bot. No heavy frameworks. (The
  analysis notebook may use pandas/matplotlib — keep those deps out of `bot/`.)
- The LLM only extracts intent; all market mapping and math is deterministic and
  auditable in `matcher.py` / `predictor.py` / `oddsapi.py` / `derive.py`.
- **Pricing cascade** (`bot/pricing.py`, `bot/pipeline.py`):
  1. API-Football odds → 2. The Odds API (player props + core) →
  3. derive (compose compounds) → 4. external web estimate (last resort).
  Try cheap/auditable sources first; only fall through when a layer can't price.
- De-vig only coherent outcome sets from the **same bookmaker and contract**.
  Compounds are composed from **separately priced components**, not from
  marginal lines of one book.
- Submit probabilities as integers **1–99**.
- Every submission path must use `pipeline.submit_with_ledger`; do not call the
  raw batch submitter from a user-facing or scheduled workflow. The SQLite
  ledger records real questions, raw odds, pricing traces and both submission
  windows. Settle it only through explicit SportPredict `current_value` outcomes
  (`python -m scripts.settle_ledger`), never by web search or score inference.
- **Determinism is required.** Every LLM call (parser + compound splitter) goes
  through `parser.chat_json`, which caches on `(PROMPT_VERSION, model, messages)`
  so the same question always maps to the same intent/source/probability across
  runs. Never add an uncached LLM call. Bump `PROMPT_VERSION` when you change
  parser semantics.
- Recurring question and compound templates are parsed deterministically. The
  parser uses `PARSER_MODEL` (default `gpt-4.1`) for unfamiliar wording only,
  with **at most one batched fallback call per match**. Because the call is
  cached, the model is a one-time cost — favour reliability over the cheapest
  model. Document any per-match cost change in the README "Cost" table.

## Quota & caching (important)
- **The Odds API is paid/metered** and **API-Football is rate-limited (450/min)**.
  Always go through `bot/cache.py` — every odds response is cached to disk under
  `cache/` (git-ignored). Never add an uncached odds fetch in a hot loop.
- The Odds API bills `markets × regions`: request only needed markets; the cache
  key is per `(event, market, regions)`. `ODDS_REGIONS` tunes breadth vs cost.
- Scheduled 30- and 5-minute submissions intentionally refresh odds once per
  window. Keep refreshes deduplicated within the run so identical Odds API
  market requests never incur repeated credits.
- The external web layer (`gpt-4.1-mini` + web search, ~$0.035/question) is the
  main spend. It is cached per question and gated by `EXTERNAL_FALLBACK` (set
  `=0` to disable). Don't run it on settled matches in backtests — a web search
  can leak the result.

## Keys / env
`config.py` loads `.env`. Required: `SPORTSPREDICT_KEY`, `APIFOOTBALL_KEY`,
`ODDS_API_KEY`, `OPENAI_API_KEY`. Mask keys in any terminal output you share.

## Test before committing
- `python run.py predict --limit 1` — cheap end-to-end smoke test (uses cache).
- `python validate.py --days 7` — backtest against settled matches.

## Useful facts
- WC2026 in API-Football: `league=1`, `season=2026`; Odds API sport
  `soccer_fifa_world_cup`.
- All providers' fixtures/events are linked by exact kickoff datetime.
- Pre-match odds are purged a few days after kickoff (both providers) — backtests
  on old fixtures price fewer markets.
