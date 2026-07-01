from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

from sportspredict.config import Settings, default_settings
from sportspredict.features.context import MatchContext
from sportspredict.rates import RateModel
from sportspredict.types import CORNERS, GOALS, OFFSIDES, SHOTS_ON_TARGET

from .assemble import load_stat_table
from .learned import FEATURE_ORDER, _row_features
from .team_ratings import TeamRatings, fit_team_ratings

DEFAULT_STAT_TABLE = "data/processed/history_stat_table.parquet"
DEFAULT_METADATA = "data/processed/rate_model.json"
ALPHA_GRID = (0.0, 0.25, 0.5, 0.75, 1.0)
_STRENGTH_POSITIVE = (GOALS, SHOTS_ON_TARGET, CORNERS, OFFSIDES)

def _monotone_cst(stat: str) -> np.ndarray:
    cst = np.zeros(len(FEATURE_ORDER), dtype=int)
    if stat in _STRENGTH_POSITIVE:
        cst[FEATURE_ORDER.index("z")] = 1
        cst[FEATURE_ORDER.index("own_elo_c")] = 1
        cst[FEATURE_ORDER.index("opp_elo_c")] = -1
    return cst

def _make_gbm(stat: str, random_state: int = 0):
    from sklearn.ensemble import HistGradientBoostingRegressor

    return HistGradientBoostingRegressor(
        loss="poisson", learning_rate=0.05, max_iter=300, max_leaf_nodes=8,
        min_samples_leaf=30, l2_regularization=1.0, early_stopping=True,
        validation_fraction=0.15, monotonic_cst=_monotone_cst(stat),
        random_state=random_state)

def assemble_training(stat_table: pd.DataFrame, ratings: TeamRatings, settings: Settings,
                      learned_stats: list[str]
                      ) -> tuple[np.ndarray, np.ndarray, dict[str, np.ndarray],
                                 dict[str, np.ndarray], np.ndarray]:
    rm = RateModel(settings)
    X_rows: list[np.ndarray] = []
    groups: list = []
    sources: list[str] = []
    y: dict[str, list[float]] = {s: [] for s in learned_stats}
    base_total: dict[str, list[float]] = {s: [] for s in learned_stats}

    for row in stat_table.itertuples(index=False):
        ta, tb = str(row.home_team), str(row.away_team)
        elo_a = float(getattr(row, "home_elo", 1500.0))
        elo_b = float(getattr(row, "away_elo", 1500.0))
        stage = str(getattr(row, "stage", "group"))
        host_a = bool(getattr(row, "home_host", False))
        host_b = bool(getattr(row, "away_host", False))
        is_ko = stage.lower() == "knockout"
        ctx = MatchContext(ta, tb, elo_a=elo_a, elo_b=elo_b, stage=stage,
                           host_a=host_a, host_b=host_b)
        base_rates = rm.build(ctx)
        group = getattr(row, "tournament", getattr(row, "match_id", 0))

        X_rows.append(_row_features(elo_a, elo_b, ta, tb, is_ko, host_a, host_b, ratings))
        X_rows.append(_row_features(elo_b, elo_a, tb, ta, is_ko, host_b, host_a, ratings))
        groups.extend([group, group])
        source = str(getattr(row, "source", "statsbomb"))
        sources.extend([source, source])
        for s in learned_stats:
            y[s].append(float(getattr(row, f"home_{s}_h1") + getattr(row, f"home_{s}_h2")))
            y[s].append(float(getattr(row, f"away_{s}_h1") + getattr(row, f"away_{s}_h2")))
            bt = base_rates.lam[s].sum(axis=1)
            base_total[s].extend([float(bt[0]), float(bt[1])])
    return (np.vstack(X_rows), np.asarray(groups),
            {s: np.asarray(v, dtype=float) for s, v in y.items()},
            {s: np.asarray(v, dtype=float) for s, v in base_total.items()},
            np.asarray(sources))

def _choose_alpha(stat, X, y_s, base_s, groups) -> tuple[float, dict[str, float]]:
    from sklearn.metrics import mean_poisson_deviance
    from sklearn.model_selection import GroupKFold

    n_groups = len(np.unique(groups))
    base_c = np.clip(base_s, 1e-6, None)
    if n_groups < 2:
        return settings_default_alpha(), {}
    ratio = y_s / base_c
    cv = GroupKFold(n_splits=min(4, n_groups))
    oof_ratio = np.empty_like(ratio, dtype=float)
    for tr, te in cv.split(X, ratio, groups):
        gbm = _make_gbm(stat)
        gbm.fit(X[tr], ratio[tr], sample_weight=base_c[tr])
        oof_ratio[te] = gbm.predict(X[te])
    oof = np.clip(base_c * np.clip(oof_ratio, 1e-6, None), 1e-6, None)
    devs: dict[str, float] = {}
    for a in ALPHA_GRID:
        blend = np.exp((1.0 - a) * np.log(base_c) + a * np.log(oof))
        devs[f"{a:.2f}"] = float(mean_poisson_deviance(y_s, blend))
    best = min(devs, key=devs.get)
    return float(best), devs

def settings_default_alpha(settings: Settings | None = None) -> float:
    s = settings or default_settings()
    return float(s.raw.get("rates", {}).get("learned", {}).get("blend_alpha", 0.5))

def train_rate_models(
    stat_table: pd.DataFrame,
    ratings: TeamRatings,
    settings: Settings,
    *,
    learned_stats: list[str] | None = None,
    artifact_path: str | Path,
    metadata_path: str | Path | None = None,
) -> tuple[dict, dict]:
    learned_stats = list(learned_stats or settings.raw["rates"]["learned"]["learned_stats"])
    X, groups, y, base_total, sources = assemble_training(
        stat_table, ratings, settings, learned_stats
    )

    lcfg = settings.raw.get("rates", {}).get("learned", {})
    max_alpha = float(lcfg.get("max_alpha", 1.0))
    _mr = lcfg.get("max_ratio")
    max_ratio = float(_mr) if _mr and float(_mr) > 1.0 else None
    stat_sources = lcfg.get("stat_sources", {}) or {}

    gbms: dict = {}
    alphas: dict[str, float] = {}
    meta_stats: dict[str, dict] = {}
    for s in learned_stats:
        allowed = stat_sources.get(s)
        mask = np.isin(sources, list(allowed)) if allowed else np.ones(len(sources), dtype=bool)
        Xs, ys = X[mask], y[s][mask]
        base_s, stat_groups = np.clip(base_total[s][mask], 1e-9, None), groups[mask]
        if not len(ys):
            print(f"  WARNING: '{s}' has no rows from sources {allowed}; skipping (alpha 0).")
            continue
        alpha_cv, devs = _choose_alpha(s, Xs, ys, base_s, stat_groups)
        gbm = _make_gbm(s)
        gbm.fit(Xs, ys / base_s, sample_weight=base_s)
        learned_mean = float(np.mean(base_s * np.clip(gbm.predict(Xs), 1e-6, None)))
        prior_mean = float(np.mean(base_s))
        ratio = learned_mean / prior_mean if prior_mean > 0 else float("inf")
        implausible = max_ratio is not None and not (1.0 / max_ratio <= ratio <= max_ratio)
        alpha = 0.0 if implausible else min(float(alpha_cv), max_alpha)
        if implausible:
            print(f"  WARNING: '{s}' learned mean {learned_mean:.3f} vs baseline prior "
                  f"{prior_mean:.3f} (ratio {ratio:.2f}) outside [1/{max_ratio:g}, "
                  f"{max_ratio:g}] -> alpha forced to 0.")
        gbms[s] = gbm
        alphas[s] = alpha
        meta_stats[s] = {
            "alpha": alpha, "alpha_cv": float(alpha_cv), "n_rows": int(len(ys)),
            "sources": list(allowed) if allowed else "all",
            "learned_mean": round(learned_mean, 4), "prior_mean": round(prior_mean, 4),
            "ratio": round(ratio, 4) if ratio != float("inf") else None,
            "plausibility_ok": not implausible, "cv_poisson_deviance": devs}

    bundle = {"gbms": gbms, "feature_names": list(FEATURE_ORDER), "alphas": alphas,
              "learned_stats": learned_stats, "target": "ratio_to_baseline", "version": 4}
    metadata = {"trained": date.today().isoformat(), "n_matches": int(len(stat_table)),
                "feature_names": list(FEATURE_ORDER), "target": "ratio_to_baseline",
                "stats": meta_stats}

    import joblib

    artifact_path = Path(artifact_path)
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(bundle, artifact_path)
    if metadata_path is not None:
        Path(metadata_path).write_text(json.dumps(metadata, indent=2))
    return bundle, metadata

def _main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Train the v0 learned Layer-1 rate models from match stats.")
    ap.add_argument("--statsbomb", default=DEFAULT_STAT_TABLE,
                    help="match stat table parquet (default: the shipped merged historical table)")
    ap.add_argument("--artifact", default=None, help="write model artifact here (default: config path)")
    ap.add_argument("--metadata", default=None, help="write model metadata here")
    ap.add_argument("--team-ratings", dest="team_ratings", default=None,
                    help="write team ratings here (default: config path)")
    args = ap.parse_args(argv)

    settings = default_settings()
    cfg = settings.raw["rates"]["learned"]

    table, results = load_stat_table(settings.path(args.statsbomb))
    print(f"[data] {len(table)} matches from {args.statsbomb}")

    print(f"[ratings] fitting team attack/defense from {len(results)} results ...")
    ratings = fit_team_ratings(results)
    ratings_path = settings.path(args.team_ratings or cfg["team_ratings"])
    ratings.save(ratings_path)

    print("[train] fitting per-stat GBMs + choosing blend alpha ...")
    artifact_path = settings.path(args.artifact or cfg["artifact"])
    metadata_path = settings.path(args.metadata or cfg.get("metadata", DEFAULT_METADATA))
    _, meta = train_rate_models(table, ratings, settings, artifact_path=artifact_path,
                                metadata_path=metadata_path)

    print(f"\nartifact -> {artifact_path}")
    print(f"team ratings -> {ratings_path}")
    for s, m in meta["stats"].items():
        devs = m["cv_poisson_deviance"]
        base = devs.get("0.00")
        chosen = devs.get(f"{m['alpha']:.2f}")
        tail = "" if base is None or chosen is None else (
            f"  cv-deviance baseline={base:.4f} -> chosen={chosen:.4f}")
        flag = "" if m.get("plausibility_ok", True) else "  [DEMOTED: implausible vs prior]"
        print(f"  {s:16s} alpha={m['alpha']:.2f}{tail}{flag}")
    return 0

if __name__ == "__main__":
    raise SystemExit(_main())
