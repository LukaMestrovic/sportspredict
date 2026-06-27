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
- The parser LLM only extracts intent. The live pricing LLM prices final markets
  from a deterministic evidence JSON plus web research. All provider market
  mapping and bookmaker probability conversion stays deterministic and auditable
  in `matcher.py` / `predictor.py` / `oddsapi.py` / `derive.py` / `evidence.py`.
- **Live pricing flow** (`bot/evidence.py`, `bot/llm_pricing.py`,
  `bot/pipeline.py`): collect direct and related API-Football / Odds API odds
  into one evidence JSON per match, include deterministic estimates only as
  labeled context, then make one cached web-grounded LLM call that returns final
  probabilities and a complete per-market audit. There are no pre-LLM anchors or
  hidden tilt math in the live submission path.
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
  so the same question always maps to the same intent across
  runs. Never add an uncached LLM call. Bump `PROMPT_VERSION` when you change
  parser semantics. The web-grounded pricing layer (`llm_pricing.py`) is the
  documented exception — non-deterministic on first call, but still cached
  (ttl=0) so re-runs are stable. The pricing prompt lives in
  `prompts/llm_pricing_prompt.md`; editing it auto-invalidates the cache through
  the prompt hash. Bump `LLM_PRICING_VERSION` when the output contract changes.
  The pricing LLM must return final `probability_int` values and complete audit
  fields for every submitted market; if a market lacks audit detail, skip it.
- Recurring question and compound templates are parsed deterministically. The
  parser uses `PARSER_MODEL` (default `gpt-5.4-mini`) for unfamiliar wording only,
  with **at most one batched fallback call per match**. Because the call is
  cached, the model is a one-time cost — favour reliability over the cheapest
  model. Document any per-match cost change in the README "Cost" table.

## Quota & caching (important)
- **The Odds API is paid/metered** and **API-Football is rate-limited (450/min)**.
  Always go through `bot/cache.py` — every odds response is cached to disk under
  `cache/` (git-ignored). Never add an uncached odds fetch in a hot loop.
- The Odds API bills `markets × regions`: request only needed markets; the cache
  key is per `(event, market, regions)`. `ODDS_REGIONS` tunes breadth vs cost.
- The scheduled 30-minute submission intentionally refreshes odds once. Keep
  refreshes deduplicated within the run so identical Odds API market requests
  never incur repeated credits.
- The final LLM pricing layer (`gpt-5.4-mini` + web search by default) is
  web-grounded spend. It is cached per match for manual repeatability and can be
  disabled with `LLM_PRICING_ENABLED=0` for deterministic validation. Scheduled
  T-30 cron fires deliberately refresh provider odds and force a fresh LLM
  pricing/web-search call for that submission window. Don't run it on settled
  matches — a web search can leak the result. `llm_pricing.price_match()` refuses
  to run once kickoff has passed, and ledger review reads frozen rows only.

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
