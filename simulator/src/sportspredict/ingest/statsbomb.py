"""StatsBomb open-data ingestion (the fitting + backtest backbone).

The key function is the **pure transform** :func:`events_to_match_stats`, which collapses a
flattened StatsBomb events frame (as returned by ``statsbombpy.sb.events``) into per-team,
per-half counts for every modelled statistic plus match-level penalties/red cards and player
goal/assist involvement. It is unit-tested on a synthetic events frame so the logic is
verifiable without a network download. :func:`build_match_stat_table` wraps it over the
configured tournaments using ``statsbombpy`` (lazy import).
"""

from __future__ import annotations

import pandas as pd

from ..config import default_settings

# Which StatsBomb on-target shot outcomes count as "shots on target".
_ON_TARGET = {"Goal", "Saved", "Saved To Post", "Saved to Post"}


def _col(events: pd.DataFrame, name: str) -> pd.Series:
    """Return a column if present, else an all-NaN series (schema is sparse)."""
    if name in events.columns:
        return events[name]
    return pd.Series([None] * len(events), index=events.index)


def events_to_match_stats(
    events: pd.DataFrame, home_team: str, away_team: str, match_id: int | None = None
) -> dict:
    """Collapse one match's events into a per-team, per-half stat record."""
    etype = _col(events, "type").astype("string")
    team = _col(events, "team").astype("string")
    period = pd.to_numeric(_col(events, "period"), errors="coerce")
    shot_outcome = _col(events, "shot_outcome").astype("string")
    shot_type = _col(events, "shot_type").astype("string")
    pass_type = _col(events, "pass_type").astype("string")
    foul_card = _col(events, "foul_committed_card").astype("string")
    bad_card = _col(events, "bad_behaviour_card").astype("string")
    foul_pen = _col(events, "foul_committed_penalty")

    rec: dict = {"match_id": match_id, "home_team": home_team, "away_team": away_team}

    def half_mask(h: int) -> pd.Series:
        return period == h

    for label, tname in (("home", home_team), ("away", away_team)):
        tmask = team == tname
        for h, suffix in ((1, "h1"), (2, "h2")):
            hm = tmask & half_mask(h)
            rec[f"{label}_goals_{suffix}"] = int(
                ((etype == "Shot") & (shot_outcome == "Goal") & hm).sum()
            )
            rec[f"{label}_shots_on_target_{suffix}"] = int(
                ((etype == "Shot") & shot_outcome.isin(_ON_TARGET) & hm).sum()
            )
            rec[f"{label}_corners_{suffix}"] = int(((pass_type == "Corner") & hm).sum())
            rec[f"{label}_fouls_{suffix}"] = int(((etype == "Foul Committed") & hm).sum())
            rec[f"{label}_offsides_{suffix}"] = int(((etype == "Offside") & hm).sum())
            cards = ((foul_card.isin({"Yellow Card", "Second Yellow"})) |
                     (bad_card.isin({"Yellow Card", "Second Yellow"}))) & hm
            rec[f"{label}_yellows_{suffix}"] = int(cards.sum())
        # Match-level rare events (any period).
        reds = ((foul_card == "Red Card") | (bad_card == "Red Card") |
                (foul_card == "Second Yellow") | (bad_card == "Second Yellow")) & tmask
        rec[f"{label}_reds"] = int(reds.sum())

    # Penalties awarded in the match (taken penalties + conceded fouls in the box).
    pens = ((shot_type == "Penalty") | (foul_pen == True)).sum()  # noqa: E712
    rec["penalties"] = int(pens)
    return rec


def player_involvement(events: pd.DataFrame) -> pd.DataFrame:
    """Per-player goals + assists in a match (priors for the player market)."""
    etype = _col(events, "type").astype("string")
    player = _col(events, "player").astype("string")
    team = _col(events, "team").astype("string")
    shot_outcome = _col(events, "shot_outcome").astype("string")
    pass_goal_assist = _col(events, "pass_goal_assist")

    goals = events[(etype == "Shot") & (shot_outcome == "Goal")]
    assists = events[pass_goal_assist == True]  # noqa: E712
    g = goals.assign(player=player[goals.index], team=team[goals.index]).groupby(
        ["team", "player"]
    ).size().rename("goals")
    a = assists.assign(player=player[assists.index], team=team[assists.index]).groupby(
        ["team", "player"]
    ).size().rename("assists")
    out = pd.concat([g, a], axis=1).fillna(0).astype(int).reset_index()
    return out


def build_match_stat_table(sample: bool = False) -> pd.DataFrame:
    """Build the per-match stat table across configured tournaments via statsbombpy."""
    try:
        from statsbombpy import sb
    except Exception as e:  # pragma: no cover - optional dependency
        raise RuntimeError(
            "statsbombpy not installed; run `uv pip install -e '.[data]'`"
        ) from e

    comps = default_settings().raw["data_sources"]["statsbomb_competitions"]
    if sample:
        comps = comps[:1]

    records = []
    for comp_id, season_id in comps:
        matches = sb.matches(competition_id=comp_id, season_id=season_id)
        for _, mrow in matches.iterrows():
            try:
                ev = sb.events(match_id=int(mrow["match_id"]))
            except Exception:
                continue
            rec = events_to_match_stats(
                ev, mrow["home_team"], mrow["away_team"], int(mrow["match_id"])
            )
            rec["competition_id"] = comp_id
            rec["season_id"] = season_id
            records.append(rec)
    return pd.DataFrame.from_records(records)
