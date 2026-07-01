"""Export the current provider market mapping in agent-readable formats.

The raw provider catalog is retained as ``soccer_live_odds_market_catalog.pdf``.
This script documents the subset of that provider space that the production
matcher currently wires to API-Football and The Odds API.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from bot import matcher


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_JSON = ROOT / "docs" / "market_catalog.json"
DEFAULT_MD = ROOT / "docs" / "market_catalog.md"
AF_THRESHOLD_RULE = "gte N -> Over N-0.5; lte N -> Under N+0.5"


def _af_entry(
    intent_market: str,
    *,
    bet_id: int,
    spec_type: str,
    subject: str = "match",
    period: str = "match",
    target: str | None = None,
    line_rule: str | None = None,
    devig: str,
    notes: str = "",
) -> dict[str, Any]:
    return {
        "provider": "api-football",
        "intent_market": intent_market,
        "subject": subject,
        "period": period,
        "market_key": f"af_bet_{bet_id}",
        "bet_id": bet_id,
        "spec_type": spec_type,
        "target": target,
        "line_rule": line_rule,
        "devig": devig,
        "notes": notes,
    }


def _oa_entry(
    intent_market: str,
    *,
    market_key: str,
    kind: str,
    subject: str = "match",
    target: str | None = None,
    line_rule: str | None = None,
    devig: str,
    notes: str = "",
) -> dict[str, Any]:
    return {
        "provider": "odds-api",
        "intent_market": intent_market,
        "subject": subject,
        "period": "match",
        "market_key": market_key,
        "kind": kind,
        "target": target,
        "line_rule": line_rule,
        "devig": devig,
        "notes": notes,
    }


def _subject_period_for_team_yesno(market: str) -> str:
    if market.endswith("_1h"):
        return "1H"
    if market.endswith("_2h"):
        return "2H"
    return "match"


def api_football_entries() -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []

    for subject, target in (("home", "Home"), ("away", "Away")):
        entries.append(_af_entry(
            "match_winner", subject=subject, bet_id=1, spec_type="select",
            target=target, devig="same-book categorical de-vig",
        ))
    entries.append(_af_entry(
        "match_draw", bet_id=1, spec_type="select", target="Draw",
        devig="same-book categorical de-vig",
    ))
    for subject, target in (("home", "Home"), ("away", "Away")):
        entries.append(_af_entry(
            "first_team_to_score", subject=subject, bet_id=14,
            spec_type="select", target=target,
            devig="same-book categorical de-vig",
            notes=(
                "Used as a labeled regulation proxy for unqualified knockout "
                "first-goal questions."
            ),
        ))
    entries.extend([
        _af_entry(
            "btts", bet_id=8, spec_type="select", target="Yes",
            devig="same-book categorical de-vig",
        ),
        _af_entry(
            "highest_scoring_half_2h", bet_id=11, spec_type="select",
            target="2nd Half", devig="same-book categorical de-vig",
        ),
        _af_entry(
            "red_card", bet_id=335, spec_type="ou", target="Over 0.5",
            line_rule="fixed Over 0.5", devig="same-book over/under de-vig",
        ),
        _af_entry(
            "own_goal", bet_id=59, spec_type="select", target="Yes",
            devig="same-book categorical de-vig",
        ),
    ])
    for subject, target in (("home", "Home"), ("away", "Away")):
        entries.append(_af_entry(
            "win_margin", subject=subject, bet_id=4, spec_type="ah",
            target=f"{target} -line", line_rule="win by N+ -> side -(N-0.5)",
            devig="same-book Asian-handicap pair de-vig",
        ))

    for market, bet_id in matcher._MATCH_OU.items():
        entries.append(_af_entry(
            market, bet_id=bet_id, spec_type="ou", line_rule=AF_THRESHOLD_RULE,
            devig="same-book over/under de-vig",
        ))
    for market, (home_id, away_id) in matcher._TEAM_OU.items():
        for subject, bet_id in (("home", home_id), ("away", away_id)):
            entries.append(_af_entry(
                market, subject=subject, bet_id=bet_id, spec_type="ou",
                line_rule=AF_THRESHOLD_RULE,
                devig="same-book over/under de-vig",
            ))
    for market, (home_id, away_id) in matcher._TEAM_YESNO.items():
        period = _subject_period_for_team_yesno(market)
        for subject, bet_id in (("home", home_id), ("away", away_id)):
            entries.append(_af_entry(
                market, subject=subject, period=period, bet_id=bet_id,
                spec_type="select", target="Yes",
                devig="same-book categorical de-vig",
            ))
    for market, bet_id in matcher._MATCH_YESNO.items():
        entries.append(_af_entry(
            market, bet_id=bet_id, spec_type="select", target="Yes",
            devig="same-book categorical de-vig",
        ))
    for market, bet_id in matcher._TEAM_SELECT.items():
        for subject, target in (("home", "Home"), ("away", "Away")):
            entries.append(_af_entry(
                market, subject=subject, bet_id=bet_id, spec_type="select",
                target=target, devig="same-book categorical de-vig",
            ))
    for market, bet_id in matcher._COMPARE.items():
        for subject, target in (("home", "Home"), ("away", "Away")):
            entries.append(_af_entry(
                market, subject=subject, bet_id=bet_id, spec_type="select",
                target=target, devig="same-book categorical de-vig",
            ))

    for market, period_map in matcher._HALF_SELECT.items():
        for period, bet_id in period_map.items():
            if market == "match_draw":
                entries.append(_af_entry(
                    market, period=period, bet_id=bet_id, spec_type="select",
                    target="Draw", devig="same-book categorical de-vig",
                ))
            else:
                for subject, target in (("home", "Home"), ("away", "Away")):
                    entries.append(_af_entry(
                        market, subject=subject, period=period, bet_id=bet_id,
                        spec_type="select", target=target,
                        devig="same-book categorical de-vig",
                    ))
    for market, period_map in matcher._HALF_MATCH_OU.items():
        for period, bet_id in period_map.items():
            notes = (
                "Provider names this half-card contract yellow-card O/U; "
                "the current matcher uses it only for half total_cards."
                if market == "total_cards" else ""
            )
            entries.append(_af_entry(
                market, period=period, bet_id=bet_id, spec_type="ou",
                line_rule=AF_THRESHOLD_RULE,
                devig="same-book over/under de-vig", notes=notes,
            ))
    for market, period_map in matcher._HALF_TEAM_OU.items():
        for period, (home_id, away_id) in period_map.items():
            for subject, bet_id in (("home", home_id), ("away", away_id)):
                entries.append(_af_entry(
                    market, subject=subject, period=period, bet_id=bet_id,
                    spec_type="ou", line_rule=AF_THRESHOLD_RULE,
                    devig="same-book over/under de-vig",
                ))
    for period, bet_id in matcher._HALF_BTTS.items():
        entries.append(_af_entry(
            "btts", period=period, bet_id=bet_id, spec_type="select",
            target="Yes", devig="same-book categorical de-vig",
        ))

    entries.extend([
        _af_entry(
            "player_goal_scorer", subject="player", bet_id=92,
            spec_type="player_yes", target="player value",
            devig="single-sided player prop haircut",
        ),
        _af_entry(
            "player_card", subject="player", bet_id=251,
            spec_type="player_yes", target="player value",
            devig="single-sided player prop haircut",
        ),
        _af_entry(
            "player_shots_on_target", subject="player", bet_id=242,
            spec_type="player_threshold", target="Player - N+",
            line_rule=AF_THRESHOLD_RULE,
            devig="single-sided player prop haircut",
        ),
    ])
    return entries


def odds_api_entries() -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for subject in ("home", "away"):
        entries.extend([
            _oa_entry(
                "match_winner", subject=subject, market_key="h2h",
                kind="multiway", target="team name",
                devig="same-book categorical de-vig",
            ),
            _oa_entry(
                "corners_compare", subject=subject, market_key="corners_1x2",
                kind="multiway", target="team name",
                devig="same-book categorical de-vig",
            ),
            _oa_entry(
                "double_chance", subject=subject, market_key="draw_no_bet",
                kind="multiway", target="team name",
                devig="same-book categorical de-vig",
                notes="Current fallback uses draw_no_bet for this intent.",
            ),
        ])
    entries.extend([
        _oa_entry(
            "btts", market_key="btts", kind="yesno", target="Yes",
            devig="same-book two-sided de-vig",
        ),
        _oa_entry(
            "total_goals", market_key="totals", kind="ou",
            line_rule=AF_THRESHOLD_RULE, devig="same-book two-sided de-vig",
        ),
        _oa_entry(
            "total_corners", market_key="alternate_totals_corners", kind="ou",
            line_rule=AF_THRESHOLD_RULE, devig="same-book two-sided de-vig",
        ),
        _oa_entry(
            "total_cards", market_key="alternate_totals_cards", kind="ou",
            line_rule=AF_THRESHOLD_RULE, devig="same-book two-sided de-vig",
        ),
        _oa_entry(
            "player_goal_scorer", subject="player",
            market_key="player_goal_scorer_anytime", kind="player_yesno",
            target="player Yes", devig="same-book two-sided de-vig if both sides exist",
        ),
        _oa_entry(
            "player_score_or_assist", subject="player",
            market_key="player_to_score_or_assist", kind="player_yesno",
            target="player Yes", devig="same-book two-sided de-vig if both sides exist",
        ),
        _oa_entry(
            "player_card", subject="player",
            market_key="player_to_receive_card", kind="player_yesno",
            target="player Yes", devig="same-book two-sided de-vig if both sides exist",
        ),
        _oa_entry(
            "player_shots_on_target", subject="player",
            market_key="player_shots_on_target", kind="player_ou",
            line_rule=AF_THRESHOLD_RULE,
            devig="same-book two-sided de-vig if both sides exist",
        ),
    ])
    return entries


def build_catalog() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "purpose": "Document the provider markets wired into the production matcher.",
        "raw_provider_catalog": "soccer_live_odds_market_catalog.pdf",
        "source_files": [
            "bot/matcher.py",
            "bot/predictor.py",
            "bot/oddsapi.py",
            "soccer_live_odds_market_catalog.pdf",
        ],
        "scope_rules": [
            (
                "API-Football and Odds API pre-match bookmaker contracts settle "
                "at regulation time unless explicitly documented otherwise."
            ),
            (
                "For knockout full-match questions, provider regulation markets "
                "are blocked except qualification and the labeled first-team-to-score proxy."
            ),
            (
                "Outcome sets are de-vigged only within the same bookmaker and "
                "coherent contract, then averaged across quoting bookmakers."
            ),
            "Half-period Odds API markets are not wired in the current matcher.",
            (
                "Unsupported, compound, and simulator-only templates must not be "
                "forced onto approximate provider contracts."
            ),
        ],
        "api_football": api_football_entries(),
        "odds_api": odds_api_entries(),
        "provider_gaps": [
            {
                "intent_market": "cards_compare",
                "reason": (
                    "No coherent all-card provider comparison is wired; the "
                    "yellow-card comparison market is intentionally not reused."
                ),
            },
            {
                "intent_market": "team_shots_on_target",
                "reason": "No direct provider mapping is wired; simulator context may handle it.",
            },
            {
                "intent_market": "none",
                "reason": "Parser uses this for compounds or templates that need non-provider handling.",
            },
        ],
    }


def _cell(value: Any) -> str:
    if value is None:
        return ""
    return str(value).replace("|", "\\|")


def _table(entries: list[dict[str, Any]], columns: list[str]) -> list[str]:
    lines = [
        "| " + " | ".join(columns) + " |",
        "| " + " | ".join("---" for _ in columns) + " |",
    ]
    for entry in entries:
        lines.append("| " + " | ".join(_cell(entry.get(col)) for col in columns) + " |")
    return lines


def render_markdown(catalog: dict[str, Any]) -> str:
    lines = [
        "# Market Catalog",
        "",
        "Generated from the current production matcher. Do not hand-edit the tables;",
        "run `python -m scripts.export_market_catalog` after matcher changes.",
        "",
        f"Raw provider catalog: `{catalog['raw_provider_catalog']}`.",
        "",
        "## Scope Rules",
        "",
    ]
    lines.extend(f"- {rule}" for rule in catalog["scope_rules"])
    lines.extend([
        "",
        "## API-Football Mappings",
        "",
    ])
    lines.extend(_table(
        catalog["api_football"],
        [
            "intent_market", "subject", "period", "market_key", "spec_type",
            "target", "line_rule", "devig", "notes",
        ],
    ))
    lines.extend([
        "",
        "## Odds API Mappings",
        "",
    ])
    lines.extend(_table(
        catalog["odds_api"],
        [
            "intent_market", "subject", "period", "market_key", "kind",
            "target", "line_rule", "devig", "notes",
        ],
    ))
    lines.extend([
        "",
        "## Provider Gaps",
        "",
    ])
    lines.extend(_table(catalog["provider_gaps"], ["intent_market", "reason"]))
    lines.append("")
    return "\n".join(lines)


def write_outputs(json_path: Path = DEFAULT_JSON, md_path: Path = DEFAULT_MD) -> None:
    catalog = build_catalog()
    json_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(catalog, indent=2, sort_keys=True) + "\n")
    md_path.write_text(render_markdown(catalog))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", type=Path, default=DEFAULT_JSON)
    parser.add_argument("--markdown", type=Path, default=DEFAULT_MD)
    args = parser.parse_args()
    write_outputs(args.json, args.markdown)
    print(f"[market-catalog] wrote {args.json} and {args.markdown}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
