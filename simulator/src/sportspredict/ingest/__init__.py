"""Data ingestion.

Each module wraps one source and returns tidy pandas frames with consistent columns; the
network/credentialed parts are isolated and lazily import optional dependencies so the core
simulator installs and runs without them. ``build_feature_tables`` orchestrates a refresh of
the cached parquet tables that Layer-1 fitting consumes.

Sources (see ``README.md``):
  * StatsBomb open data  -> per-match, per-half event stats + player involvement  [free]
  * International results -> goals/results for the strength model                  [free]
  * Elo                   -> pre-match strength                                    [free]
  * soccerdata            -> club player rates (score/assist priors)               [free]
  * API-Football          -> fixtures, lineups, referee, half splits and events    [paid]
"""

from __future__ import annotations

from pathlib import Path


def build_feature_tables(sample: bool = False, out_dir: str | Path = "data/processed") -> dict:
    """Refresh cached feature tables from available sources.

    Returns a dict of {table_name: path}. Requires the ``[data]`` extra and (for the paid
    sources) the ``APIFOOTBALL_KEY`` environment variable. With
    ``sample=True`` only a single StatsBomb tournament is ingested for a quick smoke test.
    """
    from . import statsbomb

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    written: dict[str, str] = {}

    stat_table = statsbomb.build_match_stat_table(sample=sample)
    path = out / "statsbomb_match_stats.parquet"
    stat_table.to_parquet(path)
    written["statsbomb_match_stats"] = str(path)
    return written
