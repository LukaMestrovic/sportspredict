# Bundled simulator runtime

This directory is the production-only numerical simulator used as optional
context by the pricing LLM. It is deliberately isolated from `bot/`: the live
bot stays standard-library plus `requests`, while the simulator runs in a child
Python process with its own dependency list.

Everything required at runtime is tracked here:

- `src/sportspredict/` contains the baseline generative match engine.
- `src/sphybrid/` contains learned-rate, event-timing, and report code.
- `config/` contains the model and market contract configuration.
- `data/` contains the fitted model, compact lookup tables, Elo snapshot, and
  frozen validation evidence used in audits. The validation artifact includes
  family-level rolling-origin and WC2026 Brier comparisons against always-50%
  and prior exact-contract empirical-rate baselines, with match-count warnings.

Training, ingestion, notebooks, historical raw data, and standalone competition
clients are intentionally excluded. Model artifacts are updated and reviewed in
this repository; deployment never reads a sibling checkout or prebuilt wheel.
