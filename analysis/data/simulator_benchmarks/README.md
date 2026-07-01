# Simulator benchmark exports

This directory contains the compact CSV exports needed to rebuild the tracked
simulator benchmark artifacts without reading a sibling research checkout.

The files are gzip-compressed, but the analysis scripts read either `.csv` or
`.csv.gz` with the same paths:

- `notebooks/oos_*/exotic_oos_rows.csv.gz`
- `notebooks/oos_*/exotic_empirical_rates.csv.gz`
- `notebooks/wc2026_simulator_oos_rows.csv.gz`
- `data/processed/sportspredict_question_catalog.csv.gz`

These are prediction/outcome exports and catalog metadata, not raw training
corpora or ingestion tooling.
