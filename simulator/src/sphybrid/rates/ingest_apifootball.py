"""API-Football ingestion of senior men's international matches -> the learned-rate stat table.

This is the v0 *data-scaling* path. It mirrors the exact schema of the shipped 314-match StatsBomb
table (``data/processed/history_stat_table.parquet``) so the existing ``train``/``backtest`` pipeline
consumes many more matches **with no change to how the model works** — only the input table grows.

Scope and the senior-national-team filter
------------------------------------------
We pull national-team competitions only (World Cup, Euro, Copa America, AFCON, Asian Cup, Gold Cup,
Nations Leagues, the various qualifiers, and international friendlies). Clubs, youth (U17/U20/U23),
and women's teams are dropped by two guards: an explicit non-senior name regex, and an **elo.csv
whitelist** — a match is kept only if *both* team names resolve to a name in the Elo table (which is
the martj42/eloratings national-team set the inference path also keys on). That same resolution
fixes naming differences ("Korea Republic" -> "South Korea", "Türkiye" -> "Turkey", ...) so the
learned team ratings are keyed by the names ``compete``/``predict`` look up at inference time.

What is and isn't available
---------------------------
API-Football fixture statistics are **full-match totals** (the ``half`` split is not populated here),
so we cannot recover per-half counts for shots/corners/fouls/cards. Training only ever uses the
``h1 + h2`` sum (see ``rates/train.assemble_training``), so we put the full-match total in ``*_h1``
and 0 in ``*_h2`` — exact for the learned rates. Goals are the one stat we *can* split: ``*_h1`` is
the half-time score and ``*_h2`` the rest of regulation (from ``score.fulltime``), which keeps the
goal-half markets in the offline backtest honest. ``penalties`` is not in the statistics payload and
is not a learned stat, so it is left 0.

Elo is read from the same snapshot the live bot uses (``data/raw/elo.csv``); unknown teams fall back
to 1500. All ingested matches are treated as neutral-venue (``home_host``/``away_host`` False), the
same convention the StatsBomb tournament table uses.
"""

from __future__ import annotations

import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import pandas as pd
import requests

from sportspredict.ingest import apifootball as af
from sportspredict.ingest.apifootball import _fold

# --- schema -----------------------------------------------------------------
# Per-team statistics we read, keyed by the modelled stat name. ``goals`` is handled separately
# (from the score), ``reds`` is a match-level column, the rest are per-half (total in h1).
_STAT_TYPE_MAP: dict[str, str] = {
    "Shots on Goal": "shots_on_target",
    "Corner Kicks": "corners",
    "Fouls": "fouls",
    "Offsides": "offsides",
    "Yellow Cards": "yellows",
    "Red Cards": "reds",
}
# Without these (for both teams) a fixture has no usable team-stat signal and is skipped.
_CORE_STATS = ("shots_on_target", "corners", "fouls")
_PER_HALF_STATS = ("goals", "shots_on_target", "corners", "fouls", "offsides", "yellows")

# Finished statuses whose 90-minute (``score.fulltime``) result + recorded stats we trust.
_PLAYED = {"FT", "AET", "PEN"}

# Round strings that denote knockout football (everything else -> "group"/league phase).
_KNOCKOUT_KEYS = (
    "round of", "1/8", "1/16", "quarter", "semi", "final", "3rd place", "third place",
    "play-off", "playoff", "play offs",
)

# Names that are not senior men's national teams, dropped before the Elo whitelist even looks.
_NON_SENIOR_RE = re.compile(
    r"(?:\bU-?\d{2}\b|\bU\d{2}\b|\bwomen\b|\bladies\b|\bolympic|\b[wW]$|\bgirls\b|\bboys\b|"
    r"\bamateur\b|\bxi\b|\bb team\b|\bii\b)",
    re.IGNORECASE,
)

# API-Football spelling -> elo.csv (martj42) name, for cases accent-folding can't bridge. Kept in
# sync with sportspredict.compete.team_codes so training and inference agree on the canonical name.
_ALIASES: dict[str, str] = {
    "usa": "United States",
    "korea republic": "South Korea",
    "korea dpr": "North Korea",
    "north korea dpr": "North Korea",
    "ir iran": "Iran",
    "iran ir": "Iran",
    "cote d'ivoire": "Ivory Coast",
    "cote divoire": "Ivory Coast",
    "czechia": "Czech Republic",
    "congo dr": "DR Congo",
    "dr congo": "DR Congo",
    "turkiye": "Turkey",
    "cabo verde": "Cape Verde",
    "cape verde islands": "Cape Verde",
    "china pr": "China",
    "fyr macedonia": "North Macedonia",
    "bosnia & herzegovina": "Bosnia and Herzegovina",
    "rep. of ireland": "Republic of Ireland",
    "republic of ireland": "Republic of Ireland",
    "chinese taipei": "Taiwan",
    "st. kitts and nevis": "Saint Kitts and Nevis",
    "st. vincent / grenadines": "Saint Vincent and the Grenadines",
    "st. vincent and the grenadines": "Saint Vincent and the Grenadines",
    "swaziland": "Eswatini",
    "east timor": "Timor-Leste",
    "brunei darussalam": "Brunei",
    "kosovo": "Kosovo",
}


# --- pure transforms (no network; unit-tested) ------------------------------
class Canonicalizer:
    """Resolve an API-Football team name to the Elo-table (martj42) name, or ``None`` to drop it.

    Resolution order: exact name, accent/space-folded name, curated alias. Anything that does not
    land on a known national team in ``elo_table`` (clubs, youth, women, micro-nations absent from
    the table) returns ``None`` so the caller can skip the whole match.
    """

    def __init__(self, elo_table: dict[str, float]):
        self._names = set(elo_table)
        self._fold_index = {_fold(n): n for n in self._names}

    def __call__(self, name: str) -> str | None:
        if not name or _NON_SENIOR_RE.search(name):
            return None
        if name in self._names:
            return name
        folded = _fold(name)
        if folded in self._fold_index:
            return self._fold_index[folded]
        alias = _ALIASES.get(folded)
        if alias and alias in self._names:
            return alias
        return None


def classify_stage(round_str: str | None) -> str:
    r = (round_str or "").lower()
    return "knockout" if any(k in r for k in _KNOCKOUT_KEYS) else "group"


def parse_team_stats(stats_response: list[dict]) -> dict[str, dict[str, float]] | None:
    """API-Football statistics response -> ``{team_name: {stat: total}}`` (full-match totals).

    Returns ``None`` when the core stats (shots on target / corners / fouls) are not recorded for
    both teams, so the caller skips a fixture that would otherwise be all-zero noise.
    """
    out: dict[str, dict[str, float]] = {}
    for entry in stats_response or []:
        team = (entry.get("team") or {}).get("name")
        if not team:
            continue
        rec: dict[str, float] = {}
        for item in entry.get("statistics") or []:
            stat = _STAT_TYPE_MAP.get(str(item.get("type")))
            if stat is None:
                continue
            val = item.get("value")
            rec[stat] = float(val) if val is not None else 0.0
        out[team] = rec
    if len(out) != 2:
        return None
    if any(s not in rec for rec in out.values() for s in _CORE_STATS):
        return None
    return out


def _half_goals(score: dict) -> tuple[int, int, int, int] | None:
    """(home_h1, home_h2, away_h1, away_h2) regulation goals from the score block, or ``None``."""
    ft = (score or {}).get("fulltime") or {}
    ht = (score or {}).get("halftime") or {}
    fh, fa = ft.get("home"), ft.get("away")
    if fh is None or fa is None:
        return None
    hh, ha = ht.get("home"), ht.get("away")
    if hh is None or ha is None:  # no half-time split recorded -> attribute all to the first half
        return int(fh), 0, int(fa), 0
    return int(hh), max(int(fh) - int(hh), 0), int(ha), max(int(fa) - int(ha), 0)


def fixture_row(
    fixture: dict,
    team_stats: dict[str, dict[str, float]],
    canon: Canonicalizer,
    elo_table: dict[str, float],
    tournament: str,
) -> dict | None:
    """Build one stat-table row mirroring ``history_stat_table.parquet``; ``None`` to skip."""
    teams = fixture.get("teams") or {}
    home_raw = ((teams.get("home") or {}).get("name")) or ""
    away_raw = ((teams.get("away") or {}).get("name")) or ""
    home, away = canon(home_raw), canon(away_raw)
    if home is None or away is None:
        return None
    if home_raw not in team_stats or away_raw not in team_stats:
        return None
    goals = _half_goals(fixture.get("score") or {})
    if goals is None:
        return None
    hh1, hh2, ah1, ah2 = goals

    fx = fixture.get("fixture") or {}
    row: dict = {
        "match_id": fx.get("id"),
        "home_team": home,
        "away_team": away,
        "home_goals_h1": hh1, "home_goals_h2": hh2,
        "away_goals_h1": ah1, "away_goals_h2": ah2,
    }
    hs, as_ = team_stats[home_raw], team_stats[away_raw]
    for stat in ("shots_on_target", "corners", "fouls", "offsides", "yellows"):
        row[f"home_{stat}_h1"] = float(hs.get(stat, 0.0))
        row[f"home_{stat}_h2"] = 0.0
        row[f"away_{stat}_h1"] = float(as_.get(stat, 0.0))
        row[f"away_{stat}_h2"] = 0.0
    row["home_reds"] = float(hs.get("reds", 0.0))
    row["away_reds"] = float(as_.get("reds", 0.0))
    row["penalties"] = 0
    row["competition_id"] = (fixture.get("league") or {}).get("id")
    row["season_id"] = (fixture.get("league") or {}).get("season")
    row["tournament"] = tournament
    row["match_date"] = (fx.get("date") or "")[:10]
    row["stage"] = classify_stage((fixture.get("league") or {}).get("round"))
    row["home_elo"] = float(elo_table.get(home, 1500.0))
    row["away_elo"] = float(elo_table.get(away, 1500.0))
    row["home_host"] = False
    row["away_host"] = False
    # Provenance is required for stats whose provider definitions differ. In particular,
    # StatsBomb's explicit Offside rows under-count the real event while API-Football totals do not.
    row["source"] = "apifootball"
    row["referee"] = str(fx.get("referee") or "")
    return row


def _pair_key(a: str, b: str, date: str) -> tuple:
    return (frozenset((str(a), str(b))), str(date)[:10])


def dedup_against_base(new_df: pd.DataFrame, base_df: pd.DataFrame) -> pd.DataFrame:
    """Drop rows already present in ``base_df`` (same unordered team pair + match date)."""
    if new_df.empty or base_df.empty or "match_date" not in base_df.columns:
        return new_df
    seen = {
        _pair_key(r.home_team, r.away_team, str(r.match_date))
        for r in base_df.itertuples(index=False)
    }
    keep = [
        _pair_key(r.home_team, r.away_team, str(r.match_date)) not in seen
        for r in new_df.itertuples(index=False)
    ]
    return new_df[keep].reset_index(drop=True)


def _slugify(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "", name).upper()


def tournament_label(league_name: str, season: int) -> str:
    """Stable LOTO-fold label, e.g. "World Cup" + 2022 -> "WORLDCUP2022"."""
    return f"{_slugify(league_name)}{season}"


# --- network orchestration --------------------------------------------------
# API-Football caps requests per minute (header ``x-ratelimit-limit``, 450 on the Ultra plan) and
# — critically — answers an over-limit call with HTTP 200, an empty ``response`` and a truthy
# ``errors`` block. The baseline API helper caches that empty body to disk with no TTL, poisoning the
# fixture forever. So we fetch through this validated client: a global rate gate keeps us under
# the cap, and a response is only cached when ``errors`` is empty (a genuine result, possibly an
# empty list of stats). Poisoned cache files written by an earlier run are detected and re-fetched.
class _RateGate:
    """Process-wide minimum spacing between request starts (threads share one gate)."""

    def __init__(self, min_interval: float):
        self._min_interval = min_interval
        self._lock = threading.Lock()
        self._next = 0.0

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            sleep_for = max(0.0, self._next - now)
            self._next = max(now, self._next) + self._min_interval
        if sleep_for:
            time.sleep(sleep_for)


_GATE = _RateGate(min_interval=0.15)  # ~6.5 req/s -> ~400/min, under the 450/min cap


def _cache_path(endpoint: str, params: dict) -> Path:
    return af._CACHE / (endpoint.replace("/", "_") + "_" + af._slug(params) + ".json")


def _valid(data: dict | None) -> bool:
    """A usable API response: present and without a truthy ``errors`` block (rate-limit/plan error)."""
    return bool(data) and not data.get("errors")


def _api_get(endpoint: str, params: dict, *, retries: int = 5) -> dict:
    """Validated, rate-limited, disk-cached GET. Never returns/caches a rate-limit response."""
    af._CACHE.mkdir(parents=True, exist_ok=True)
    cache_file = _cache_path(endpoint, params)
    if cache_file.exists():
        try:
            cached = json.loads(cache_file.read_text())
        except Exception:
            cached = None
        if _valid(cached):
            return cached  # genuine cached result (resume without re-billing)
    last_err = ""
    for attempt in range(retries):
        _GATE.wait()
        resp = requests.get(f"{af._BASE}/{endpoint}", params=params,
                            headers={"x-apisports-key": af._key()}, timeout=30)
        if resp.status_code == 429:
            time.sleep(2.0 * (attempt + 1))
            continue
        resp.raise_for_status()
        data = resp.json()
        if _valid(data):
            cache_file.write_text(json.dumps(data))
            return data
        last_err = str(data.get("errors"))  # rate-limit / transient plan error -> back off, retry
        time.sleep(2.0 * (attempt + 1))
    raise RuntimeError(f"{endpoint} {params} failed after {retries} tries: {last_err}")


def discover_competitions(league_ids: list[int], min_year: int) -> list[tuple[int, str, int]]:
    """(league_id, league_name, season) for every season >= ``min_year`` of each league."""
    data = _api_get("leagues", {"country": "World"})["response"]
    by_id = {item["league"]["id"]: item for item in data}
    out: list[tuple[int, str, int]] = []
    for lid in league_ids:
        item = by_id.get(lid)
        if not item:
            continue
        name = item["league"]["name"]
        for s in item.get("seasons", []):
            year = int(s["year"])
            if year >= min_year:
                out.append((lid, name, year))
    return out


def fetch_fixtures(league: int, season: int) -> list[dict]:
    return _api_get("fixtures", {"league": league, "season": season})["response"]


def _fetch_stats(fixture_id: int) -> list[dict]:
    return _api_get("fixtures/statistics", {"fixture": int(fixture_id)})["response"]


def build_table(
    competitions: list[tuple[int, str, int]],
    *,
    elo_table: dict[str, float],
    max_workers: int = 8,
    limit: int | None = None,
    progress: bool = True,
) -> pd.DataFrame:
    """Fetch fixtures + statistics for the given competitions and build the stat table.

    Senior-national-team filtering happens *before* any statistics call (from the fixture team
    names) so quota is spent only on kept matches. All API responses are disk-cached by the baseline
    client path, so re-running resumes instead of re-billing.
    """
    canon = Canonicalizer(elo_table)
    rows: list[dict] = []
    for lid, lname, season in competitions:
        try:
            fixtures = fetch_fixtures(lid, season)
        except Exception as e:  # pragma: no cover - network
            if progress:
                print(f"  [skip] league {lid} season {season}: {e}")
            continue
        label = tournament_label(lname, season)
        # Keep only finished senior-vs-senior fixtures; resolve names up front (no API call yet).
        todo: list[dict] = []
        for fx in fixtures:
            if ((fx.get("fixture") or {}).get("status") or {}).get("short") not in _PLAYED:
                continue
            teams = fx.get("teams") or {}
            if canon((teams.get("home") or {}).get("name") or "") is None:
                continue
            if canon((teams.get("away") or {}).get("name") or "") is None:
                continue
            todo.append(fx)
        if limit is not None:
            todo = todo[: max(0, limit - len(rows))]
        if progress:
            print(f"  {label:28s} fixtures={len(fixtures):4d} senior-played={len(todo):4d}")

        kept = 0
        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            futs = {ex.submit(_fetch_stats, fx["fixture"]["id"]): fx for fx in todo}
            for fut in as_completed(futs):
                fx = futs[fut]
                try:
                    stats = parse_team_stats(fut.result())
                except Exception:
                    stats = None
                if stats is None:
                    continue
                row = fixture_row(fx, stats, canon, elo_table, label)
                if row is not None:
                    rows.append(row)
                    kept += 1
        if progress:
            print(f"  {' ':28s} -> {kept} rows with stats")
        if limit is not None and len(rows) >= limit:
            break
    return pd.DataFrame.from_records(rows)


def merge_tables(base: pd.DataFrame, new: pd.DataFrame) -> pd.DataFrame:
    """StatsBomb base + de-duped API-Football rows with provider provenance preserved."""
    base = base.copy()
    if "source" not in base:
        base["source"] = "statsbomb"
    if "referee" not in base:
        base["referee"] = ""
    new = new.copy()
    if "source" not in new:
        new["source"] = "apifootball"
    if "referee" not in new:
        new["referee"] = ""
    new = dedup_against_base(new, base)
    combined = pd.concat([base, new], ignore_index=True)
    # Keep the base column order; any base-only columns become NaN for new rows (e.g. none today).
    return combined[[c for c in base.columns if c in combined.columns]
                    + [c for c in combined.columns if c not in base.columns]]


# Default senior men's international competitions (national teams only). Seasons are discovered per
# league from ``--min-year``. Overridable via settings ``data_sources.apifootball_leagues``.
DEFAULT_LEAGUES: list[int] = [
    1,    # World Cup
    4,    # Euro Championship
    5,    # UEFA Nations League
    6,    # Africa Cup of Nations
    7,    # Asian Cup
    9,    # Copa America
    10,   # Friendlies (clubs/youth dropped by the whitelist)
    21,   # Confederations Cup
    22,   # CONCACAF Gold Cup
    29, 30, 31, 32, 33, 34, 37,   # World Cup Qualification (all confederations + play-offs)
    35,   # Asian Cup Qualification
    36,   # Africa Cup of Nations Qualification
    536,  # CONCACAF Nations League
    913,  # CONMEBOL-UEFA Finalissima
    960,  # Euro Qualification
]
DEFAULT_MIN_YEAR = 2018
STATSBOMB_BASE = "data/processed/statsbomb_stat_table.parquet"
APIFOOTBALL_OUT = "data/processed/apifootball_stat_table.parquet"
HISTORY_OUT = "data/processed/history_stat_table.parquet"


def _main(argv: list[str] | None = None) -> int:
    import argparse

    from sportspredict.config import default_settings
    from sportspredict.ingest.elo import load_elo_table

    from ..compete import load_dotenv

    ap = argparse.ArgumentParser(
        description="Ingest senior men's international matches from API-Football into the "
        "learned-rate stat table (mirrors the StatsBomb schema; no model change).")
    ap.add_argument("--min-year", type=int, default=None, help=f"earliest season (default {DEFAULT_MIN_YEAR})")
    ap.add_argument("--leagues", default=None, help="comma-separated league ids (default: curated set)")
    ap.add_argument("--elo-csv", default="data/raw/elo.csv")
    ap.add_argument("--base", default=STATSBOMB_BASE, help="StatsBomb base table to merge onto")
    ap.add_argument("--out", default=APIFOOTBALL_OUT, help="write the API-Football-only table here")
    ap.add_argument("--history-out", default=HISTORY_OUT, help="write the merged training table here")
    ap.add_argument("--max-workers", type=int, default=8)
    ap.add_argument("--limit", type=int, default=None, help="cap rows (smoke test)")
    ap.add_argument("--no-merge", action="store_true", help="write only the API-Football table")
    args = ap.parse_args(argv)

    settings = default_settings()
    load_dotenv(settings.root)  # make APIFOOTBALL_KEY available without exporting it by hand
    ds = settings.raw.get("data_sources", {})
    min_year = args.min_year or int(ds.get("apifootball_min_year", DEFAULT_MIN_YEAR))
    league_ids = ([int(x) for x in args.leagues.split(",")] if args.leagues
                  else list(ds.get("apifootball_leagues", DEFAULT_LEAGUES)))

    elo_table = load_elo_table(settings.path(args.elo_csv))
    print(f"[discover] leagues={league_ids} min_year={min_year}")
    comps = discover_competitions(league_ids, min_year)
    print(f"[discover] {len(comps)} (league, season) competitions to scan")

    new = build_table(comps, elo_table=elo_table, max_workers=args.max_workers, limit=args.limit)
    print(f"\n[ingest] {len(new)} senior international rows with stats")
    out_path = settings.path(args.out)
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    new.to_parquet(out_path)
    print(f"[ingest] API-Football table -> {out_path}")

    if args.no_merge:
        return 0
    base = pd.read_parquet(settings.path(args.base))
    merged = merge_tables(base, new)
    merged.to_parquet(settings.path(args.history_out))
    print(f"[ingest] merged {len(base)} base + {len(merged) - len(base)} new "
          f"= {len(merged)} rows -> {settings.path(args.history_out)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
