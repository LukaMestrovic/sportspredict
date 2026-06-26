# sportspredict-llm

A minimal **v0** LLM-based bot that predicts event probabilities for the
**SportPredict × Jump Trading Probability Cup** (FIFA World Cup 2026).

It reads the binary questions SportPredict asks for each upcoming match, builds a
per-match odds/context evidence file, and asks one web-grounded LLM call to
produce final probabilities plus a full audit trail for every submitted market.

## Architecture

The **Parser** turns each question into a structured intent. The bot then
collects direct and related bookmaker odds into an auditable evidence JSON; the
LLM receives that file in its prompt, searches for additional online odds and
match context, and returns final YES probabilities.

```
   question ─▶ parser ─▶ evidence JSON ─▶ web-grounded LLM final pricing ─▶ 1–99
                         ▲
                         └─ API-Football, The Odds API, related markets,
                            deterministic estimates, lineups, venue, referee
```

1. **Parser** ([bot/parser.py](bot/parser.py)) — recurring competition templates
   are parsed deterministically. Only unfamiliar wording is sent to `gpt-4.1`,
   in at most **one batched call per match**. That fallback is cached on
   `(model, prompt version, questions)`, so a question maps to the same intent
   on every re-run. Known
   compound forms are also split locally; novel compounds use the same cached
   LLM path.
2. **Matcher** ([bot/matcher.py](bot/matcher.py)) — maps an intent to a specific
   provider market (API-Football bet ID or Odds API key) from
   [soccer_live_odds_market_catalog.pdf](soccer_live_odds_market_catalog.pdf),
   including full-match and 1st/2nd-half contracts.
3. **Evidence builder** ([bot/evidence.py](bot/evidence.py)) — writes one JSON
   file per match under `logs/llm_pricing_runs/`. Direct mapped markets include
   every per-book de-vigged probability, source, bookmaker, raw odds and de-vig
   method. Questions without direct odds receive relevant related odds and
   labeled deterministic estimates, not hidden anchors.
4. **LLM pricing** ([bot/llm_pricing.py](bot/llm_pricing.py)) — one cached
   web-grounded call per match. The model must price every SportPredict market
   from the evidence JSON plus online research, including Kalshi, Polymarket,
   Pinnacle, Betfair and betting platforms where available. Every returned
   market must include final probability, provided odds used, online odds found,
   non-odds factors, downweighted evidence, sources and a concise public
   reasoning summary. Markets missing that audit are skipped.

Provider fixtures/events are linked by **kickoff datetime and both teams**;
kickoff alone is not unique when group-stage matches start simultaneously. The
API-Football fixture supplies canonical home/away names.

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env     # then fill in your keys
```

`.env` (git-ignored) holds four keys:

| Key | Purpose |
|-----|---------|
| `SPORTSPREDICT_KEY` | SportPredict Probability Cup API (`sp_live_…`) |
| `APIFOOTBALL_KEY`   | API-Football v3 (api-sports.io direct host) |
| `ODDS_API_KEY`      | The Odds API (the-odds-api.com) — **paid/metered**, cached |
| `OPENAI_API_KEY`    | parser fallback + compound splitter + final LLM pricing |

Optional LLM pricing env: `LLM_PRICING_MODEL` picks the model (default
`gpt-5.5`); `LLM_PRICING_ENABLED=0` disables final LLM pricing for local
deterministic/backtest-style runs.

## Usage

```bash
# Predict every open match and print results (no submission)
python run.py predict

# Only the first open match (cheap end-to-end check)
python run.py predict --limit 1

# Deterministic backtest-style preview (no final LLM pricing)
python run.py predict --limit 1 --no-llm

# Predict and submit to SportPredict
python run.py predict --submit
```

### Deployment (autonomous, isolated from your working tree)

```bash
scripts/deploy.sh          # build the immutable image + install the cron
tail -f logs/cron.log      # watch it tick
scripts/run.sh --status    # what is the next match / ETA?
```

`scripts/deploy.sh` builds a Docker image (`sportspredict-llm:v1`) with the
**current** source baked in, then installs a per-minute cron that runs that image
via `scripts/run.sh`. Each tick is a dispatcher: a fast no-op until a match is
within 30 minutes, then it upserts predictions once at the **30-minute** mark
before kickoff — where the lineups are out and the LLM pricing layer has ~30
minutes of headroom to research online odds and context. Each scheduled fire
refreshes provider odds, fetches the latest available lineups, forces a fresh LLM
pricing/web-search call for that window, and writes an evidence JSON plus full
Markdown/JSON audit under `logs/llm_pricing_runs/`. Manual development runs
still use the cached LLM price for repeatability unless the prompt/model/cache
key changes.

Because the code is baked into the image, the running bot is a **frozen
snapshot** — editing the working tree (or running tests, dev predictions, etc.)
never affects a live tick. To ship changes, re-run `scripts/deploy.sh` (idempotent:
it rebuilds the image and rewrites only the `sportspredict-llm` cron block). The
container mounts `cache/` (paid odds + parser cache, cron markers) and `logs/`
(ledger, audit) so state persists across ticks; secrets are read from `.env` at
run time and never baked into the image.

### Validation

```bash
python validate.py --days 7
```

Runs the deterministic API-Football/empirical validation path against every
**settled** WC2026 fixture from the last 7 days: reconstructs SportPredict-style
questions, settles them from the final score and match statistics, and reports
the Brier score. Final LLM pricing is disabled here to prevent web-search result
leakage, so validation measures the local evidence/model machinery rather than
the live LLM pricing layer. Latest historical run before the redesign:

```
Fixtures:           27
Predictions priced: 216
Bot mean Brier:     0.2088   (lower is better)
Coin-flip Brier:    0.2500   (always 50%)
Directional acc.:   63%
```

> API-Football purges pre-match odds a few days after kickoff, so fresh
> backtests on older fixtures may price fewer markets. Cached pre-match odds
> remain usable.

### Prediction ledger

Every submission is recorded in `logs/prediction_ledger.sqlite3` before it is
sent. The local SQLite ledger keeps one row per real question per submission
window, including:

- event, lobby, match, fixture, kickoff, observation and submission times;
- parser/model version, structured intent and attempted provider market spec;
- raw API-Football and Odds API snapshots observed for the run;
- evidence JSON path/hash and the full LLM audit/report paths;
- each bookmaker's de-vigged probability observed by the run;
- final probability, source, per-question LLM audit JSON and reasoning summary;
- skipped questions and their reasons; and
- eventual binary outcome and Brier score.

Scheduled runs are tagged `30`; manual submissions use `-1`. A failed API
submission remains in the ledger with status `failed` and does not create a cron
marker.

Review one match after kickoff — every prediction, outcome, Brier score, audit
paths and LLM reasoning summary:

```bash
python -m scripts.settle_ledger --match "Portugal"   # by id or name substring
```

Settle completed real questions and print overall, per-window and per-source
performance:

```bash
python -m scripts.settle_ledger
```

Settlement is idempotent. It joins by SportPredict `market_id`, reads the
explicit `current_value` outcome from the settled web API, and stores the
authenticated result metadata. This avoids trying to infer an outcome from a
Brier score, which is ambiguous for a 50% prediction. The ledger is git-ignored
runtime data and should be retained across deployments.

## Caching & quota

The Odds API is **paid and metered**; API-Football is flat-rate but rate-limited
(450/min). Every odds response is cached to disk under `cache/` (git-ignored):

- **Odds API** — one cache entry per `(event, market, regions)`; a market is
  fetched at most once per TTL (12 h). The Odds API bills `markets × regions`, so
  the bot requests only the markets a match actually needs and reuses the cache
  across questions and re-runs. Clear with `python -c "from bot.cache import clear; clear()"`.
- **API-Football** — fixtures (1 h) and per-fixture odds (6 h) cached, with
  rate-limit backoff; final statistics used by backtests are cached permanently.

The autonomous submitter deliberately bypasses the odds TTL once at the
30-minute window. Each refresh replaces the disk entry and is deduplicated in
memory within that run, so the submission sees the latest market movement and
newly opened props without repeated identical provider calls.

`ODDS_REGIONS` (default `eu,uk,us`) controls breadth vs cost;
`LLM_PRICING_ENABLED=0` disables the final web-grounded pricing layer for local
deterministic checks.

## Cost

SportPredict is free; API-Football is a flat-rate subscription. Metered costs:

| Source | Unit cost | Per match (first run) | Whole tournament* |
|---|---|---|---|
| Parser `gpt-4.1` (cached fallback) | $2.00/$8.00 per 1M tok | $0 known; ≤$0.004 unfamiliar | ≤$0.46 |
| Compound splitter `gpt-4.1` (cached fallback) | same | $0 known; ≤$0.001 unfamiliar | ≤$0.10 |
| Odds API | billed `markets×regions` (even empty markets) | ~3–4 markets × 3 regions ≈ **9–12 credits** (single 30-min window) | within plan |
| Final LLM pricing `gpt-5.5` (1 cached call/match, multi-source web research) | varies with evidence size + web calls | observed smoke **~$1.15** before further compaction | roughly **$50–120** |

*104 matches. **Every LLM call is cached**, and ordinary odds re-runs reuse the
disk cache; only the single scheduled submission window deliberately refreshes
odds. Caching the parser on `(model, prompt, questions)` also makes unfamiliar
question→intent mapping deterministic across runs. Final LLM pricing is one
cached web-grounded call per match; re-runs reuse the frozen pre-match audit.
Parser + splitter spend is well under **$1**; final pricing cost depends mostly
on evidence size, output audit detail and web-search calls. The first live smoke
with `gpt-5.5` logged about `$1.15`; evidence compaction keeps future calls
smaller, but auditability is intentionally prioritized over minimum token spend.

## Supported markets

Full match **and** 1st/2nd half: match result, total goals, BTTS, team total
goals, total & team corners, corners 1x2, total & team cards, team-to-score;
plus half draws, card comparisons, and (full match) offsides totals,
offsides/fouls/shots-on-target 1x2, total shots on target, highest-scoring-half;
player anytime-scorer, score-or-assist, shots-on-target, cards (both providers);
compounds, penalties/red cards and unusual questions through related evidence.
If the LLM does not return a complete audit for a market, that market is skipped
rather than submitted without reviewability.

## Evidence and Audit

Every live match writes an evidence JSON before the LLM call. For directly
mapped questions, it lists every provider/bookmaker probability for the exact
contract. For questions without direct odds, it lists relevant related
probabilities: component legs for compounds, team/match totals, compare markets,
player props, half/full-match cousins and other match context odds. Existing
derive/empirical models are retained as labeled context only, never as hidden
final anchors.

The LLM response is also persisted as JSON and Markdown. For each market it must
state:

- final `probability_int`;
- provided odds used, with how and why;
- online odds found independently, with probability conversion method and URL;
- tactics, weather, lineup, referee, motivation and other non-odds factors used;
- evidence ignored/downweighted; and
- a concise reasoning summary.

### Deterministic model context

Some unsupported contracts still get deterministic estimates inside the evidence
file. These estimates are auditable context for the LLM, not submitted prices.

- **Shots on target:** invert a quoted match-total O/U probability into a
  Poisson rate. Split it between teams with
  `home_share = clamp(0.5 + 0.40·(P(home more) − P(away more)), 0.2, 0.8)` from
  shots-on-target 1x2. Allocate 45% to the first half and 55% to the second.
- **Half shots-on-target comparison:** "more shots on target than the opponent
  in the 1st/2nd half" has no bookmaker market (the full-match SoT 1x2 is priced
  directly). Split each team's match SoT rate into the half (45%/55%) and price
  the lead with competing Poisson counts.
- **Shot-count calibration:** apply `logit(p_cal) = logit(p_raw) − 0.18`. On 224
  recent settled team/threshold checks (3+ through 6+), mean Brier was **0.164**
  versus **0.250** for a coin flip. Raw probabilities were about four points
  above observed frequencies before this single-intercept correction.
- **Player half shots:** infer a Poisson rate from the quoted full-match player
  probability, then apply the appropriate half share.
- **Half cards:** infer the half total-card rate and allocate it with the half
  cards 1x2 market; fall back to full-match team-card rates scaled 42%/58%.
- **First scorer in 2H:** convert both team-to-score 2H probabilities to rates
  and use competing Poisson processes, including the chance of no second-half goal.
- **Penalty/red card:** calibrate the closest single-sided penalty/red quotes;
  their OR union subtracts an enlarged intersection for positive correlation.
  In live LLM evidence, the two supported penalty markets ("penalty kick
  awarded" and "penalty kick awarded OR red card shown") also receive optional
  `simulator_model_estimates` from `../sportspredict-hybrid`: the bridge runs
  that repo's own virtualenv, imports its `src/` tree ahead of any installed
  package so local hybrid edits are picked up immediately, and passes the
  learned-rate simulator output to the LLM as context only.

## LLM pricing layer

[bot/llm_pricing.py](bot/llm_pricing.py) makes one web-grounded call per match.
Its instruction template lives at
[prompts/llm_pricing_prompt.md](prompts/llm_pricing_prompt.md); editing the
prompt changes the prompt hash and re-keys future calls. The cache key is
`(version, model, match_id, prompt_hash)`, so a re-run uses the frozen pre-match
audit rather than researching after the result is known. The layer refuses to run
once kickoff has passed.

The model is not allowed to emit hidden adjustments. It must return a complete
public audit for every `market_id`; otherwise that market is skipped. Review the
full evidence and audit after a run with:

```bash
python -m scripts.settle_ledger --match "Portugal"
```

## Notebook: bot vs. crowd

[notebooks/bot_vs_crowd.ipynb](notebooks/bot_vs_crowd.ipynb) scores the bot
against the **crowd mean** on settled markets. The crowd consensus is hidden by
the bot REST API but exposed for settled markets by the SportPredict *web* API
(`POST /probability/match-crowd-stats` → `prediction_average` + `current_value`;
see [bot/web.py](bot/web.py)). Latest historical deterministic run
(12 most-recent settled matches; final LLM pricing off to avoid result leakage):

```
Head-to-head questions: 61
Bot mean Brier:   0.2272     Crowd mean Brier: 0.2278     Coin-flip: 0.25
Bot beats crowd on 32/61 (52%)
```

The bot is on par with the crowd on the markets it prices, using only de-vigged
odds (and ahead of it on the higher-confidence, multi-book subset).
Notebook deps (`pandas matplotlib jupyter`) are separate from the pure-stdlib
bot — install with `uv pip install pandas matplotlib jupyter nbconvert`.

## Layout

```
bot/
  config.py        keys + constants
  cache.py         persistent on-disk cache (quota efficiency)
  ledger.py        SQLite prediction traces + real-result settlement
  sportspredict.py SportPredict REST client (/api/v1)
  web.py           SportPredict web API (/api) — crowd stats for settled markets
  apifootball.py   API-Football client + cached fixtures/odds/statistics
  oddsapi.py       The Odds API client + de-vig + per-book observations
  parser.py        deterministic templates + cached LLM fallback → intent
  matcher.py       intent → API-Football / Odds API market spec (catalog)
  predictor.py     API-Football odds → de-vigged observations
  evidence.py      one JSON evidence bundle per match
  llm_pricing.py   web-grounded final probabilities + per-market audit
  pricing.py       deterministic intent pricing (validation/evidence helper)
  derive.py        compounds + empirical correlated-signal context
  external.py      legacy web estimate helper for deterministic disabled mode
  pipeline.py      orchestration: parse → evidence → LLM pricing → submit
run.py             CLI: predict / submit / --no-llm deterministic preview
validate.py        settled-match backtest (vs realized outcomes)
scripts/
  predict_log.py   local JSON/Markdown prediction snapshots
  cron_submit.py   scheduled 30-minute submission + LLM audit
  settle_ledger.py settle real questions and report live Brier scores
  run.sh           cron-safe virtualenv wrapper
prompts/
  llm_pricing_prompt.md  designed instruction template for final pricing
notebooks/
  bot_vs_crowd.ipynb   bot vs crowd-mean post-mortem on settled markets
```
