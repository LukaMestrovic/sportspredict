# sportspredict-llm

A minimal **v0** LLM-based bot that predicts event probabilities for the
**SportPredict × Jump Trading Probability Cup** (FIFA World Cup 2026).

It reads the binary questions SportPredict asks for each upcoming match and
prices each through a **layered fallback cascade**, so almost nothing is skipped.

## Architecture

The **Parser** turns each question into a structured intent; the intent is then
priced by the first source in the cascade that can cover it:

```
                    ┌─ Templates, then cached LLM fallback ─ structured intent
   question ───────▶│
                    └─ price via cascade ▼
   ┌────────────────────────────────────────────────────────────────┐
   │ 1. API-Football odds   de-vig bookmaker market (primary)        │
   │ 2. The Odds API        player props + core markets AF lacks     │  → prob
   │ 3. Derive              compounds + empirical signal models       │   (1–99)
   │ 4. External (web)      web-grounded LLM estimate (last resort)   │
   └────────────────────────────────────────────────────────────────┘
```

1. **Parser** ([bot/parser.py](bot/parser.py)) — recurring competition templates
   are parsed deterministically. Only unfamiliar wording is sent to `gpt-4.1`,
   in at most **one batched call per match**. That fallback is cached on
   `(model, prompt version, questions)`, so a question maps to the same intent —
   and therefore the same source and probability — on every re-run. Known
   compound forms are also split locally; novel compounds use the same cached
   LLM path.
2. **Matcher** ([bot/matcher.py](bot/matcher.py)) — maps an intent to a specific
   provider market (API-Football bet ID or Odds API key) from
   [soccer_live_odds_market_catalog.pdf](soccer_live_odds_market_catalog.pdf),
   including full-match and 1st/2nd-half contracts.
3. **Pricing cascade** ([bot/pricing.py](bot/pricing.py), [bot/pipeline.py](bot/pipeline.py)):
   - **API-Football** ([bot/predictor.py](bot/predictor.py)) — de-vig the coherent
     outcome set per book, average the fair probability across books.
   - **The Odds API** ([bot/oddsapi.py](bot/oddsapi.py)) — fallback adding player
     anytime-scorer, score-or-assist, shots-on-target and cards that
     API-Football rarely quotes.
   - **Derive** ([bot/derive.py](bot/derive.py)) — split a compound ("A AND/OR B"),
     price each component through the cascade and combine it; or estimate an
     unsupported contract from correlated API-Football markets.
   - **External** ([bot/external.py](bot/external.py)) — last resort: a web-grounded
     LLM estimate (prediction markets, news, stats) for questions no odds source
     covers (e.g. team/half shots-on-target totals, 2nd-half comparisons).

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
| `OPENAI_API_KEY`    | LLM parser + compound splitter + external web estimate |

## Usage

```bash
# Predict every open match and print results (no submission)
python run.py predict

# Only the first open match (cheap end-to-end check)
python run.py predict --limit 1

# Predict and submit to SportPredict
python run.py predict --submit
```

### Validation

```bash
python validate.py --days 7
```

Runs the production API-Football and empirical pricing path against every
**settled** WC2026 fixture from the last 7 days: reconstructs SportPredict-style
questions, settles them from the final score and match statistics, and reports
the Brier score. The paid Odds API is not queried and web fallback is forcibly
disabled to prevent result leakage. Latest run:

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
- each bookmaker's de-vigged probability, final probability, source and label;
- skipped questions and their reasons; and
- eventual binary outcome and Brier score.

Scheduled runs are tagged `30` or `5`; manual submissions use `-1`. A failed
API submission remains in the ledger with status `failed` and does not create a
cron marker.

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

The autonomous submitter deliberately bypasses the odds TTL once at both the
30-minute and 5-minute windows. Each refresh replaces the disk entry and is
deduplicated in memory within that run, so the final submission sees late market
movement and newly opened props without repeated identical provider calls.

`ODDS_REGIONS` (default `eu,uk`) controls breadth vs cost; `EXTERNAL_FALLBACK=0`
disables the paid web layer entirely.

## Cost

SportPredict is free; API-Football is a flat-rate subscription. Metered costs:

| Source | Unit cost | Per match (first run) | Whole tournament* |
|---|---|---|---|
| Parser `gpt-4.1` (cached fallback) | $2.00/$8.00 per 1M tok | $0 known; ≤$0.004 unfamiliar | ≤$0.46 |
| Compound splitter `gpt-4.1` (cached fallback) | same | $0 known; ≤$0.001 unfamiliar | ≤$0.10 |
| Odds API | flat sub, billed `markets×regions` | normal cache; refreshed at 30/5 min | within plan |
| External web search `gpt-4.1-mini` | ~$0.035 / question | **off by default** | **$0** (opt-in) |

*104 matches. **Every LLM call is cached**, and ordinary odds re-runs reuse the
disk cache; only the two scheduled submission windows deliberately refresh
odds. Caching the parser on `(model, prompt, questions)` also makes unfamiliar
question→source mapping deterministic across runs. The web layer is **off by
default** (`EXTERNAL_FALLBACK=0`) — it is non-deterministic and the empirical
layer covers its cases from bookmaker odds at prediction time. Total tournament
LLM spend is well under **$1**; enable the web layer (`EXTERNAL_FALLBACK=1`) only
if you accept ~$15–20 and a non-deterministic last resort.

## Supported markets

Full match **and** 1st/2nd half: match result, total goals, BTTS, team total
goals, total & team corners, corners 1x2, total & team cards, team-to-score;
plus half draws, card comparisons, and (full match) offsides totals,
offsides/fouls/shots-on-target 1x2, total shots on target, highest-scoring-half;
player anytime-scorer, score-or-assist, shots-on-target, cards (both providers);
compounds (derived). Anything else routes to the external estimate.

## Coverage cascade

Each question is priced by the first layer that can cover it. On the audited
live set (**318 questions / 32 matches**, 2026-06-22), with both the paid Odds
API and web layer disabled:

| Layer | Source | Priced | Typical questions |
|---|---|---:|---|
| 1 | API-Football | 170 | match/half result, totals, corners/cards/offsides/fouls, total shots on target, player props |
| 2 | The Odds API | not queried | preserved the paid quota during this audit |
| 3 | Compound derive | 17 | "BTTS **AND** 3+ goals", first-score **and** half-score |
| 3 | Empirical derive | 50 | team/half shots, half cards, first 2H scorer, penalty/red |
| — | Skipped | 81 | required signals unavailable or direct mapped contracts not yet quoted |

So 237/318 are priced without either paid fallback. The remainder are skipped on
this audited *far-future* set because their direct contracts (or empirical
signals) are not quoted yet; at prediction time (~30 min before kickoff) those
deep markets are live, so the empirical layer prices them. A lone-book
API-Football quote on a normally-deep market is treated as a likely mis-map and
only trusted if it isn't an extreme — it otherwise cascades onward.

The cascade is **deterministic and auditable end-to-end**: the parser is cached
so each question maps to a fixed intent across runs; compounds are detected by
explicit conjunctions; half-period and comparison questions map to their exact
half/1x2 contracts (or, where no contract exists, to a fixed empirical model)
rather than a full-match line. The web layer is **off by default**, so nothing
reaches a non-deterministic estimate unless explicitly opted in
(`EXTERNAL_FALLBACK=1`); with it off, an unpriceable question is skipped, never
guessed.

Derivation uses **independence** to combine components — `P(A AND B)=P(A)·P(B)`,
`P(A OR B)=P(A)+P(B)−P(A)·P(B)` — an approximation, but far better than skipping.

### Empirical derivation

Unsupported contracts are estimated only after direct pricing fails. This layer
uses the API-Football bookmaker payload already loaded for the match and never
calls The Odds API, so it adds no paid odds credits.

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

## Notebook: bot vs. crowd

[notebooks/bot_vs_crowd.ipynb](notebooks/bot_vs_crowd.ipynb) scores the bot
against the **crowd mean** on settled markets. The crowd consensus is hidden by
the bot REST API but exposed for settled markets by the SportPredict *web* API
(`POST /probability/match-crowd-stats` → `prediction_average` + `current_value`;
see [bot/web.py](bot/web.py)). Latest run (12 most-recent settled matches; odds
cascade through layer 3, web layer off to avoid result leakage on past matches):

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
  oddsapi.py       The Odds API client (fallback) + de-vig
  parser.py        deterministic templates + cached LLM fallback → intent
  matcher.py       intent → API-Football / Odds API market spec (catalog)
  predictor.py     API-Football odds → de-vigged probability
  pricing.py       price one intent through the AF → Odds API cascade
  derive.py        compounds + empirical correlated-signal models
  external.py      web-grounded LLM estimate (last resort)
  pipeline.py      orchestration: AF → Odds API → derive/empirical → external
run.py             CLI: predict / submit
validate.py        settled-match backtest (vs realized outcomes)
scripts/
  predict_log.py   local JSON/Markdown prediction snapshots
  cron_submit.py   scheduled 30- and 5-minute submissions
  settle_ledger.py settle real questions and report live Brier scores
  run.sh           cron-safe virtualenv wrapper
notebooks/
  bot_vs_crowd.ipynb   bot vs crowd-mean post-mortem on settled markets
```
