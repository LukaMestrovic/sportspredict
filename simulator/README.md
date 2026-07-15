# Bundled simulator runtime

This directory is the production numerical runtime used as deterministic model
context by the parent bot. It is deliberately isolated from `bot/` and runs in a
child Python process with its own pinned dependencies.

The tracked runtime consists of:

- `src/sportspredict/`: the generative match engine and market resolvers.
- `src/sphybrid/`: the learned-rate loader, event timing, player allocation,
  compact report builder, and JSON bridge.
- `config/`: model and market-contract settings.
- `data/`: fitted artifacts, compact lookups, the Elo snapshot, and frozen
  historical validation evidence used in audits.

Training tables, ingestion clients, fitting code, notebooks, and offline
backtests are intentionally excluded. The fitted artifacts are immutable runtime
inputs; retraining happens outside this repository.

The parent bot invokes the bridge directly. A local smoke test is:

```bash
printf '%s\n' '{"home":"France","away":"Morocco","questions":[{"market_id":"q1","question":"Will a penalty kick be awarded in the match?"}],"n_sims":100}' \
  | PYTHONPATH=simulator/src SPORTSPREDICT_ROOT=simulator \
    python -m sphybrid.bridge
```

The bridge is the only executable interface retained in this runtime package.

## Exact empirical specials

The runtime resolves two recurring event contracts directly:

- a regulation goal in either first- or second-half added time;
- the first regulation card occurring before the first regulation goal (card
  with no goal is Yes; neither event is No).

Their frozen evidence contains exact all-history and historical-knockout labels.
At target time, `bot/wc2026_evidence.py` adds WC2026 and WC2026-knockout labels
from final matches strictly before kickoff. `bot/empirical_specials.py` removes
the known early-WC2026 overlap from the frozen cohort, constructs four disjoint
era-by-stage cells, and fits a deterministic penalized binomial logit. This is
the primary simulator baseline when no exact bookmaker contract is available;
the raw Monte Carlo union/race estimate remains disclosed as a contract check.
