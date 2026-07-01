"""Ingest API-Football per-player fixture stats -> ``player_stat_table.parquet``.

This builds the per-(player, fixture) table that the share fit
(``fit_shares.py``) and the allocation backtest (``backtest_players.py``)
consume, so the ``postsim.player_allocation`` layer can be validated before it
is enabled.

It reuses the team ingest's rate-limited, validated, disk-cached fetcher (``_api_get``) and the
national-team ``Canonicalizer``, and only visits fixtures already kept in ``history_stat_table``
(``source == apifootball``), so no new quota is spent on matches we dropped. API-Football returns
``null`` (not ``0``) for a player with no recorded count; the feasibility probe showed the summed
player shots-on **reconciles to the team total ~98%** of the time, so ``null`` is treated as ``0``
and a per-(fixture, team) ``reconciles_sot`` flag records whether the sum matches the team's
recorded shots-on-target total (the trust filter the fit/backtest use).
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

from sportspredict.config import default_settings
from sportspredict.ingest.elo import load_elo_table

from ..rates.ingest_apifootball import Canonicalizer, _api_get

# API-Football positions (G/D/M/F) -> the simulator's player_stat_lambda position keys.
_POS = {"G": "GK", "D": "DF", "M": "MF", "F": "FW"}

_COUNT_COLS = ["shots_total", "shots_on", "goals", "assists", "fouls", "offsides",
               "yellows", "penalties_won"]


def _num(x) -> float:
    """API-Football count: ``null`` means the player recorded none -> 0.0."""
    return float(x) if x is not None else 0.0


def _fetch_players(fixture_id: int) -> list[dict]:
    return _api_get("fixtures/players", {"fixture": int(fixture_id)})["response"]


def parse_fixture_players(response: list[dict], canon: Canonicalizer,
                          home: str, away: str) -> list[dict]:
    """Per-player rows for the two recognised national-team sides of one fixture (else skipped)."""
    rows: list[dict] = []
    for tm in response or []:
        cn = canon((tm.get("team") or {}).get("name", "") or "")
        side = "home" if cn == home else "away" if cn == away else None
        if side is None:
            continue
        prows: list[dict] = []
        team_sot = 0.0
        for pl in tm.get("players") or []:
            st = ((pl.get("statistics") or [{}]) or [{}])[0] or {}
            games = st.get("games") or {}
            shots = st.get("shots") or {}
            goals = st.get("goals") or {}
            cards = st.get("cards") or {}
            fouls = st.get("fouls") or {}
            pen = st.get("penalty") or {}
            son = _num(shots.get("on"))
            team_sot += son
            prows.append({
                "player": (pl.get("player") or {}).get("name"),
                "player_id": (pl.get("player") or {}).get("id"),
                "team": cn,
                "team_side": side,
                "position": _POS.get(games.get("position"), "FW"),
                "minutes": _num(games.get("minutes")),
                "substitute": bool(games.get("substitute")),
                "shots_total": _num(shots.get("total")),
                "shots_on": son,
                "goals": _num(goals.get("total")),
                "assists": _num(goals.get("assists")),
                "fouls": _num(fouls.get("committed")),
                "offsides": _num(st.get("offsides")),
                "yellows": _num(cards.get("yellow")),
                "penalties_won": _num(pen.get("won")),
            })
        for r in prows:
            r["team_sot_sum"] = team_sot
        rows.extend(prows)
    return rows


# Explicit Arrow schema so every streamed chunk writes identically (the dev host has no swap, so
# the table is flushed in chunks rather than held in one ~130k-row list — that OOM-kills the box).
_SCHEMA_FIELDS = [
    ("player", "string"), ("player_id", "int64"), ("team", "string"), ("team_side", "string"),
    ("position", "string"), ("minutes", "float64"), ("substitute", "bool_"),
    *[(c, "float64") for c in _COUNT_COLS],
    ("match_id", "int64"), ("tournament", "string"), ("team_sot", "float64"), ("reconciles_sot", "bool_"),
]


def _schema():
    import pyarrow as pa  # noqa: PLC0415

    return pa.schema([(n, getattr(pa, t)()) for n, t in _SCHEMA_FIELDS])


def build_player_table(history: pd.DataFrame, canon: Canonicalizer, out_path, *,
                       limit: int | None = None, chunk: int = 400, progress: bool = True) -> int:
    """Fetch ``fixtures/players`` for every API-Football fixture and stream the per-player table.

    Rows are flushed to ``out_path`` every ``chunk`` fixtures so peak memory stays bounded (the
    swapless dev host OOM-kills a single ~130k-row build). The team's recorded shots-on-target
    (``home/away_shots_on_target_h1+h2``) comes from ``history`` so each row carries
    ``reconciles_sot`` = (Σ player shots_on == team total). Returns the total row count.
    """
    import pyarrow as pa  # noqa: PLC0415
    import pyarrow.parquet as pq  # noqa: PLC0415

    af = (
        history[history["source"] == "apifootball"].copy()
        if "source" in history else history.copy()
    )
    af["so_home"] = af["home_shots_on_target_h1"] + af["home_shots_on_target_h2"]
    af["so_away"] = af["away_shots_on_target_h1"] + af["away_shots_on_target_h2"]
    if limit:
        af = af.head(limit)
    n = len(af)
    schema = _schema()
    writer = pq.ParquetWriter(str(out_path), schema)
    buf: list[dict] = []
    total = miss = rec_true = rec_tot = 0

    def flush():
        nonlocal buf
        if buf:
            writer.write_table(pa.Table.from_pylist(buf, schema=schema))
            buf = []

    try:
        for i, row in enumerate(af.itertuples(index=False), 1):
            try:
                resp = _fetch_players(int(row.match_id))
                prows = parse_fixture_players(resp, canon, row.home_team, row.away_team)
            except Exception:
                prows = []
            if not prows:
                miss += 1
            else:
                team_total = {"home": float(row.so_home), "away": float(row.so_away)}
                seen_side = set()
                for r in prows:
                    r["match_id"] = int(row.match_id)
                    r["tournament"] = str(row.tournament)
                    tt = team_total[r["team_side"]]
                    r["team_sot"] = tt
                    r["reconciles_sot"] = bool(r.pop("team_sot_sum") == tt)
                    if r["team_side"] not in seen_side:
                        seen_side.add(r["team_side"])
                        rec_tot += 1
                        rec_true += int(r["reconciles_sot"])
                buf.extend(prows)
                total += len(prows)
            if i % chunk == 0:
                flush()
            if progress and (i % 250 == 0 or i == n):
                print(f"[players] {i}/{n} fixtures  rows={total}  missing={miss}", flush=True)
        flush()
    finally:
        writer.close()
    if progress:
        rate = rec_true / rec_tot if rec_tot else 0.0
        print(f"[players] {total} rows, {miss} missing; SoT reconciles on {rate:.1%} of team-sides",
              flush=True)
    return total


def _load_dotenv(root: str | Path) -> None:
    import os

    path = Path(root) / ".env"
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Ingest API-Football per-player fixture stats.")
    ap.add_argument("--history", default="data/processed/history_stat_table.parquet")
    ap.add_argument("--elo-csv", dest="elo_csv", default="data/raw/elo.csv")
    ap.add_argument("--out", default="data/processed/player_stat_table.parquet")
    ap.add_argument("--limit", type=int, default=None)
    args = ap.parse_args(argv)

    settings = default_settings()
    _load_dotenv(settings.root)
    canon = Canonicalizer(load_elo_table(settings.path(args.elo_csv)))
    history = pd.read_parquet(settings.path(args.history))
    out_path = settings.path(args.out)
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    total = build_player_table(history, canon, out_path, limit=args.limit)
    print(f"[players] wrote {total} rows -> {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
