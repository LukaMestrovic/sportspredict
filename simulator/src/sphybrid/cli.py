from __future__ import annotations

import argparse
import json
import sys

from sportspredict.config import default_settings
from sportspredict.features.context import MatchContext

from .engine import build_engine

def _add(p, specs):
    for flags, kwargs in specs:
        p.add_argument(*flags, **kwargs)

def cmd_predict(args) -> int:
    engine = build_engine(default_settings())
    ctx = MatchContext(
        args.team_a, args.team_b, elo_a=args.elo_a, elo_b=args.elo_b, stage=args.stage,
        host_a=args.host_a, host_b=args.host_b,
    )
    pred = engine.predict(
        ctx, args.question,
        market_odds=json.loads(args.odds_json) if args.odds_json else None,
        n_sims=args.n_sims,
    )
    print(f"[rate model: {type(engine.rate_model).__name__}]")
    print(f"Q: {pred.question}")
    print(f"  market : {pred.market}  {pred.params}")
    print(f"  p_model: {pred.p_model:.4f}")
    if pred.p_market is not None:
        print(f"  p_market: {pred.p_market:.4f}")
    print(f"  p_final: {pred.p_final:.4f}   ({pred.notes}; {pred.n_sims} sims)")
    return 0

def cmd_train(args) -> int:
    from .rates.train import _main

    argv = ["--statsbomb", args.statsbomb]
    for name, flag in (("artifact", "--artifact"), ("metadata", "--metadata"),
                       ("team_ratings", "--team-ratings")):
        if getattr(args, name, None) is not None:
            argv += [flag, getattr(args, name)]
    return _main(argv)

def cmd_ingest(args) -> int:
    from .rates.ingest_apifootball import _main

    argv: list[str] = []
    for name, flag in (("min_year", "--min-year"), ("leagues", "--leagues"), ("base", "--base"),
                       ("out", "--out"), ("history_out", "--history-out"),
                       ("max_workers", "--max-workers"), ("limit", "--limit")):
        if getattr(args, name, None) is not None:
            argv += [flag, str(getattr(args, name))]
    if args.no_merge:
        argv.append("--no-merge")
    return _main(argv)

def cmd_backtest(args) -> int:
    from .backtest import _main

    argv = ["--stat-table", args.stat_table, "--n-sims", str(args.n_sims)]
    if args.in_sample:
        argv.append("--in-sample")
    if getattr(args, "held", None):
        argv += ["--held-tournaments", args.held]
    if getattr(args, "learned_stats", None):
        argv += ["--learned-stats", args.learned_stats]
    if args.as_json:
        argv.append("--json")
    return _main(argv)

def cmd_validate_timeline(args) -> int:
    from .postsim.validate import _main

    argv = ["--elo-csv", args.elo_csv, "--n-sims", str(args.n_sims)]
    if args.limit is not None:
        argv += ["--limit", str(args.limit)]
    if args.held:
        argv += ["--held-tournaments", args.held]
    if args.as_json:
        argv.append("--json")
    return _main(argv)

def cmd_simulation_report(args) -> int:
    from .report import simulation_report_from_payload

    if args.input == "-":
        payload = json.load(sys.stdin)
    else:
        with open(args.input, encoding="utf-8") as fh:
            payload = json.load(fh)
    report = simulation_report_from_payload(payload, settings=default_settings())
    print(json.dumps(report, ensure_ascii=False, indent=2 if args.pretty else None, sort_keys=True))
    return 0

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="sphybrid")
    sub = p.add_subparsers(dest="cmd", required=True)

    pr = sub.add_parser("predict", help="probability for a single question")
    _add(pr, [
        (("--team-a",), {"dest": "team_a", "required": True}),
        (("--team-b",), {"dest": "team_b", "required": True}),
        (("--elo-a",), {"dest": "elo_a", "type": float, "default": 1500.0}),
        (("--elo-b",), {"dest": "elo_b", "type": float, "default": 1500.0}),
        (("--stage",), {"default": "group", "choices": ["group", "knockout"]}),
        (("--host-a",), {"dest": "host_a", "action": "store_true"}),
        (("--host-b",), {"dest": "host_b", "action": "store_true"}),
        (("--question",), {"required": True}),
        (("--odds-json",), {"dest": "odds_json", "default": None}),
        (("--n-sims",), {"dest": "n_sims", "type": int, "default": None}),
    ])
    pr.set_defaults(func=cmd_predict)

    tr = sub.add_parser("train", help="learn Layer-1 rates from the historical match table")
    _add(tr, [
        (("--statsbomb",), {"default": "data/processed/history_stat_table.parquet"}),
        (("--artifact",), {"default": None}),
        (("--metadata",), {"default": None}),
        (("--team-ratings",), {"dest": "team_ratings", "default": None}),
    ])
    tr.set_defaults(func=cmd_train)

    ig = sub.add_parser("ingest", help="ingest senior international matches from API-Football "
                                       "into the learned-rate stat table")
    _add(ig, [
        (("--min-year",), {"dest": "min_year", "type": int, "default": None}),
        (("--leagues",), {"default": None, "help": "comma-separated league ids (default: curated set)"}),
        (("--base",), {"default": None, "help": "StatsBomb base table to merge onto"}),
        (("--out",), {"default": None, "help": "write the API-Football-only table here"}),
        (("--history-out",), {"dest": "history_out", "default": None,
                              "help": "write the merged training table here"}),
        (("--max-workers",), {"dest": "max_workers", "type": int, "default": None}),
        (("--limit",), {"type": int, "default": None, "help": "cap rows (smoke test)"}),
        (("--no-merge",), {"dest": "no_merge", "action": "store_true"}),
    ])
    ig.set_defaults(func=cmd_ingest)

    bt = sub.add_parser("backtest", help="offline Brier comparison of baseline vs learned rates")
    _add(bt, [
        (("--stat-table",), {"dest": "stat_table", "default": "data/processed/history_stat_table.parquet"}),
        (("--n-sims",), {"dest": "n_sims", "type": int, "default": 4000}),
        (("--in-sample",), {"dest": "in_sample", "action": "store_true"}),
        (("--held-tournaments",), {"dest": "held", "default": None,
                                   "help": "comma-separated tournaments to score as held-out folds"}),
        (("--learned-stats",), {"dest": "learned_stats", "default": None,
                                 "help": "comma-separated learned-stat ablation"}),
        (("--json",), {"dest": "as_json", "action": "store_true"}),
    ])
    bt.set_defaults(func=cmd_backtest)

    vt = sub.add_parser(
        "validate-timeline",
        help="Brier/calibration of goal timing against cached API-Football events",
    )
    _add(vt, [
        (("--elo-csv",), {"dest": "elo_csv", "default": "data/raw/elo.csv"}),
        (("--n-sims",), {"dest": "n_sims", "type": int, "default": 4000}),
        (("--limit",), {"type": int, "default": None}),
        (("--held-tournaments",), {"dest": "held", "default": None}),
        (("--json",), {"dest": "as_json", "action": "store_true"}),
    ])
    vt.set_defaults(func=cmd_validate_timeline)

    sr = sub.add_parser(
        "simulation-report",
        help="compact question-scoped simulator evidence JSON for an LLM pricing layer",
    )
    _add(sr, [
        (("--input",), {"default": "-", "help": "bridge JSON path, or - for stdin"}),
        (("--pretty",), {"action": "store_true"}),
    ])
    sr.set_defaults(func=cmd_simulation_report)
    return p

def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)

if __name__ == "__main__":
    raise SystemExit(main())
