"""Auditable live-odds proxies for simulator-supported no-direct contracts.

The exact-contract path remains in :mod:`bot.matcher` and :mod:`bot.predictor`.
This module is deliberately narrower: for a small allowlist of recurring
contracts it identifies one related, liquid API-Football contract, asks the
simulator for that same helper contract, and transfers only a capped share of
the live market's log-odds residual to the target simulator estimate.

The related quote is never relabelled as a price for the target.  Every
bookmaker observation, the different source contract, the transfer coefficient,
and the per-book transformed target estimate remain visible in the returned
evidence.  No provider request is made here; ``ctx.af_books`` must already hold
the retained API-Football snapshot.
"""
from __future__ import annotations

import math
import re
import statistics
from dataclasses import dataclass
from typing import Iterable, Mapping

from .matcher import match_intent
from .odds_context import PriceCtx
from .predictor import observations as af_observations
from .teams import same_team


PROXY_SCHEMA_VERSION = 1
HELPER_ID_PREFIX = "__live_odds_proxy__"

# Prevent a thin or settlement-mismatched helper quote from overwhelming the
# exact-target simulator.  The relation-specific alpha is applied after this
# cap, so the largest possible target movement is materially smaller still.
MAX_ABS_HELPER_LOGIT_GAP = 1.5
_EPSILON = 1e-6


@dataclass(frozen=True)
class _Recipe:
    key: str
    relation_type: str
    relation_note: str
    alpha: float
    helper_question: str
    helper_intent: dict


def helper_targets(
    markets: Iterable[Mapping],
    intents: Mapping,
    home: str,
    away: str,
) -> dict[str, dict]:
    """Return one synthetic simulator helper market/intent per eligible target.

    The result is keyed by the *target* market ID.  Each value has ``market``
    and ``intent`` members, making integration explicit and avoiding accidental
    attachment of a helper report to a SportPredict market::

        groups = helper_targets(markets, intents, home, away)
        helper_markets = [group["market"] for group in groups.values()]
        helper_intents = {
            group["market"]["id"]: group["intent"]
            for group in groups.values()
        }

    Callers should send the helper markets to the simulator with an empty
    direct-odds entry.  The live helper quote is collected later, solely from
    the already-fetched API-Football snapshot.
    """
    intents = intents or {}
    out: dict[str, dict] = {}
    for market in markets:
        if not isinstance(market, Mapping) or market.get("id") is None:
            continue
        raw_market_id = market["id"]
        market_id = str(raw_market_id)
        intent = (
            intents.get(raw_market_id)
            or intents.get(market_id)
            or {}
        )
        question = str(market.get("question") or "")
        recipe = _recipe_for(question, intent, home, away)
        if recipe is None:
            continue
        helper_id = _helper_id(market_id, recipe.key)
        out[market_id] = {
            "market": {
                "id": helper_id,
                "question": recipe.helper_question,
            },
            "intent": dict(recipe.helper_intent),
            "recipe_id": recipe.key,
        }
    return out


def build_proxy_and_blend(
    target_market: Mapping,
    question: str,
    intent: Mapping | None,
    target_simulator_estimate: Mapping | None,
    helper_simulator_estimates: Mapping[str, Mapping] | None,
    ctx: PriceCtx,
) -> tuple[dict | None, dict | None]:
    """Build labeled proxy evidence and its simulator-centered baseline.

    Returns ``(None, None)`` unless all three inputs needed for an auditable
    transfer exist:

    * a supported exact-target simulator estimate (or its selected calibrated
      baseline),
    * a simulator estimate for the same helper contract quoted by the book, and
    * at least one usable API-Football observation for that helper contract.

    The helper market is related to, but different from, the target contract.
    The returned ``blended_baseline`` is therefore a model-assisted proxy, not a
    bookmaker probability for the SportPredict question.
    """
    if not isinstance(target_market, Mapping) or target_market.get("id") is None:
        return None, None
    question = str(question or target_market.get("question") or "")
    intent = intent or {}
    recipe = _recipe_for(question, intent, ctx.home, ctx.away)
    if recipe is None:
        return None, None

    target_probability, target_source = _target_probability(
        target_simulator_estimate,
    )
    if target_probability is None:
        return None, None

    target_market_id = str(target_market["id"])
    helper_id = _helper_id(target_market_id, recipe.key)
    helper_estimate = (helper_simulator_estimates or {}).get(helper_id)
    helper_probability = _estimate_probability(helper_estimate)
    if helper_probability is None:
        return None, None

    helper_spec = match_intent(
        recipe.helper_intent,
        ctx.home,
        ctx.away,
        stage=ctx.stage,
    )
    if helper_spec is None:
        return None, None
    provider_observations = af_observations(ctx.af_books, helper_spec)
    if not provider_observations:
        return None, None

    retained_observations: list[dict] = []
    per_book: list[dict] = []
    for observation in provider_observations:
        live_probability = _observation_probability(observation)
        if live_probability is None:
            continue
        raw_gap = _logit(live_probability) - _logit(helper_probability)
        capped_gap = max(
            -MAX_ABS_HELPER_LOGIT_GAP,
            min(MAX_ABS_HELPER_LOGIT_GAP, raw_gap),
        )
        blended_probability = _logistic(
            _logit(target_probability) + recipe.alpha * capped_gap,
        )

        retained = dict(observation)
        retained.update({
            "role": "live_odds_proxy",
            "is_direct_for_target": False,
            "target_contract_match": False,
            "relation_type": recipe.relation_type,
        })
        retained_observations.append(retained)
        per_book.append({
            "source": observation.get("source") or "api-football",
            "bookmaker": observation.get("bookmaker") or "unknown",
            "market_key": observation.get("market_key"),
            "helper_probability_pct": round(live_probability * 100.0, 2),
            "raw_helper_logit_gap": round(raw_gap, 6),
            "capped_helper_logit_gap": round(capped_gap, 6),
            "applied_target_logit_adjustment": round(
                recipe.alpha * capped_gap, 6,
            ),
            "blended_target_probability_pct": round(
                blended_probability * 100.0, 2,
            ),
        })

    if not per_book:
        return None, None

    book_probabilities = [
        item["blended_target_probability_pct"] / 100.0 for item in per_book
    ]
    central_probability = statistics.median(book_probabilities)
    minimum_probability = min(book_probabilities)
    maximum_probability = max(book_probabilities)
    helper_contract_key = (
        helper_estimate.get("contract_key")
        if isinstance(helper_estimate, Mapping) else None
    )
    formula = (
        "logit(p_target_blend_book) = logit(p_target_simulator_center) + "
        "alpha * clip(logit(p_live_helper_book) - "
        f"logit(p_simulator_helper), +/-{MAX_ABS_HELPER_LOGIT_GAP:g})"
    )

    live_proxy = {
        "schema_version": PROXY_SCHEMA_VERSION,
        "recipe_id": recipe.key,
        "target_market_id": target_market_id,
        "target_question": question,
        "is_direct_odds": False,
        "target_contract_match": False,
        "no_marginal_relabel": True,
        "relation": {
            "type": recipe.relation_type,
            "note": recipe.relation_note,
            "alpha": recipe.alpha,
        },
        "helper_contract": {
            "market_id": helper_id,
            "question": recipe.helper_question,
            "intent": dict(recipe.helper_intent),
            "provider_spec": dict(helper_spec),
            "simulator_contract_key": helper_contract_key,
            "simulator_probability_pct": round(
                helper_probability * 100.0, 2,
            ),
        },
        "observations": retained_observations,
        "blend_formula": formula,
        "warning": (
            "These observations price the disclosed helper contract, not the "
            "SportPredict target. Use only through the labeled simulator-centered "
            "blend; never report them as direct target odds."
        ),
    }
    blended_baseline = {
        "source": "live_odds_proxy_plus_simulator",
        "probability_pct": round(central_probability * 100.0, 2),
        "target_simulator_center_pct": round(
            target_probability * 100.0, 2,
        ),
        "target_simulator_center_source": target_source,
        "helper_simulator_probability_pct": round(
            helper_probability * 100.0, 2,
        ),
        "relation_type": recipe.relation_type,
        "alpha": recipe.alpha,
        "max_abs_helper_logit_gap": MAX_ABS_HELPER_LOGIT_GAP,
        "formula": formula,
        "aggregation": (
            "Median of retained per-book transformed target estimates; the full "
            "book range is retained below."
        ),
        "book_count": len(per_book),
        "probability_pct_range": {
            "min": round(minimum_probability * 100.0, 2),
            "max": round(maximum_probability * 100.0, 2),
        },
        "per_book_estimates": per_book,
        "evidence_role": "proxy_simulator_blend",
        "is_direct_odds": False,
    }
    return live_proxy, blended_baseline


def _recipe_for(
    question: str,
    intent: Mapping,
    home: str,
    away: str,
) -> _Recipe | None:
    lower = str(question or "").lower()
    market = intent.get("market")
    subject = intent.get("subject")

    # The direct component lines are preferable.  When they are unavailable,
    # total SOT is a necessary but insufficient condition for both teams clearing
    # the same threshold; the simulator preserves the joint allocation.
    if (
        market == "none"
        and ("each team" in lower or "both teams" in lower)
        and "shot" in lower and "on target" in lower
    ):
        threshold = _threshold_at_least(lower)
        if threshold is not None and threshold >= 1:
            combined = threshold * 2
            return _Recipe(
                key=f"each_team_sot_{threshold}_via_total_{combined}",
                relation_type="necessary_total_for_joint_team_threshold",
                relation_note=(
                    f"Both teams recording {threshold}+ SOT requires {combined}+ "
                    "total SOT, but the total alone does not ensure both teams clear "
                    "the threshold. The target simulator retains that allocation risk."
                ),
                alpha=0.45,
                helper_question=(
                    f"Will there be {combined} or more total shots on target in "
                    "regulation?"
                ),
                helper_intent=_intent(
                    "total_shots_on_target", comparator="gte",
                    threshold=combined,
                ),
            )

    # An eventual winner/placement contract is not a regulation 1X2 contract.
    # The API-Football qualification quote is close, but stays labeled as a scope
    # proxy because the target wording is not literally qualification.
    if (
        market == "match_winner"
        and subject in {"home", "away"}
        and intent.get("time_scope") == "full_match"
    ):
        team = home if subject == "home" else away
        return _Recipe(
            key=f"eventual_winner_{subject}_via_qualification",
            relation_type="eventual_outcome_scope_proxy",
            relation_note=(
                "The helper is the provider's two-way To Qualify outcome, used as "
                "a proxy for the named team winning the placement/final outcome. "
                "It is not a regulation match-winner quote."
            ),
            alpha=0.70,
            helper_question=f"Will {team} advance?",
            helper_intent=_intent(
                "to_advance", subject=subject, comparator="yes",
                time_scope="full_match",
            ),
        )

    # These three intents have deliberately accepted near-contract provider
    # mappings in matcher.py.  They enter this module when evidence collection
    # reclassifies those mappings out of exact direct odds, so a companion
    # simulator estimate can preserve the small settlement difference.
    if (
        market in {"cards_compare", "team_cards"}
        and intent.get("comparator") == "more"
        and subject in {"home", "away"}
    ):
        team = home if subject == "home" else away
        opponent = away if subject == "home" else home
        return _Recipe(
            key=f"all_cards_compare_{subject}_via_yellow_cards",
            relation_type="yellow_card_1x2_for_all_cards_comparison",
            relation_note=(
                "The helper is the provider's yellow-card/bookings 1x2 market. "
                "The target compares all cards, so rare red-card settlement "
                "differences remain with the target simulator."
            ),
            alpha=0.65,
            helper_question=(
                f"Will {team} receive more yellow cards than {opponent} in "
                "regulation?"
            ),
            helper_intent=_intent(
                "cards_compare", subject=subject, comparator="more",
            ),
        )

    if (
        market == "team_score"
        and subject in {"home", "away"}
        and bool(intent.get("excludes_own_goals"))
    ):
        team = home if subject == "home" else away
        return _Recipe(
            key=f"team_score_no_own_{subject}_via_scoreboard",
            relation_type="scoreboard_team_goal_for_no_own_goal_contract",
            relation_note=(
                "The helper is the scoreboard team-to-score market. The target "
                "excludes goals credited as opponent own goals, so that rare "
                "settlement difference remains with the target simulator."
            ),
            alpha=0.85,
            helper_question=(
                f"Will {team} score at least 1 goal in regulation?"
            ),
            helper_intent=_intent(
                "team_score", subject=subject, comparator="yes",
            ),
        )

    if (
        market == "first_team_to_score"
        and subject in {"home", "away"}
        and intent.get("time_scope") == "full_match"
    ):
        team = home if subject == "home" else away
        return _Recipe(
            key=f"full_match_first_goal_{subject}_via_regulation",
            relation_type="regulation_first_goal_for_full_match_first_goal",
            relation_note=(
                "The helper settles on the regulation first goal. The target can "
                "also settle on an extra-time first goal after a scoreless 90 "
                "minutes; that narrow tail remains in the target simulator."
            ),
            alpha=0.85,
            helper_question=(
                f"Will {team} score the first goal in regulation?"
            ),
            helper_intent=_intent(
                "first_team_to_score", subject=subject, comparator="yes",
            ),
        )

    if market == "player_score_or_assist" and intent.get("player"):
        player = str(intent["player"])
        return _Recipe(
            key="player_score_or_assist_via_scorer",
            relation_type="player_scorer_component",
            relation_note=(
                "Anytime scorer is one component of score-or-assist. Player markets "
                "may also be void on non-participation, unlike SportPredict; the low "
                "transfer weight leaves lineup exposure with the target simulator."
            ),
            alpha=0.35,
            helper_question=f"Will {player} score a goal in regulation?",
            helper_intent=_intent(
                "player_goal_scorer", subject="player", player=player,
                comparator="yes", excludes_own_goals=True,
            ),
        )

    if market == "first_goal_assisted":
        return _Recipe(
            key="first_goal_assisted_via_any_goal",
            relation_type="goal_occurrence_base_for_assisted_first_goal",
            relation_note=(
                "An assisted first goal requires a regulation goal. The helper "
                "does not price whether the first goal has a credited assist; the "
                "target simulator retains that attribution conditional."
            ),
            alpha=0.35,
            helper_question="Will there be at least 1 goal in regulation?",
            helper_intent=_intent(
                "total_goals", comparator="gte", threshold=1,
            ),
        )

    if market == "team_two_plus_same_half":
        threshold = _integer(intent.get("threshold"))
        if threshold is not None and threshold >= 2:
            return _Recipe(
                key=f"team_{threshold}plus_same_half_via_total_{threshold}",
                relation_type="necessary_total_for_team_same_half_threshold",
                relation_note=(
                    f"One team scoring {threshold}+ goals in one half requires at "
                    f"least {threshold} regulation goals. The helper does not "
                    "encode their team or half allocation."
                ),
                alpha=0.30,
                helper_question=(
                    f"Will there be {threshold} or more total goals in regulation?"
                ),
                helper_intent=_intent(
                    "total_goals", comparator="gte", threshold=threshold,
                ),
            )

    if market == "penalty_scored":
        return _Recipe(
            key="penalty_scored_via_penalty_awarded",
            relation_type="penalty_awarded_super_event_for_penalty_scored",
            relation_note=(
                "A scored penalty requires a penalty to be awarded. The helper "
                "does not price conversion, rescission, or a missed penalty; that "
                "conditional remains in the target simulator."
            ),
            alpha=0.60,
            helper_question="Will a penalty kick be awarded in regulation?",
            helper_intent=_intent("penalty_awarded"),
        )

    if market == "player_sot_compare" and intent.get("period", "match") == "match":
        first_subject = _first_compared_player_subject(
            question, intent, home, away,
        )
        if first_subject is not None:
            team = home if first_subject == "home" else away
            opponent = away if first_subject == "home" else home
            return _Recipe(
                key=f"player_sot_compare_via_team_sot_{first_subject}",
                relation_type="first_player_team_sot_comparison_driver",
                relation_note=(
                    f"The helper prices {team} recording more team SOT than its "
                    "opponent. It is only a volume and game-state driver for the "
                    "first named player beating the second; player allocation is "
                    "left with the target simulator."
                ),
                alpha=0.25,
                helper_question=(
                    f"Will {team} record more shots on target than {opponent} "
                    "in regulation?"
                ),
                helper_intent=_intent(
                    "shots_on_target_compare", subject=first_subject,
                    comparator="more",
                ),
            )

    if (
        market == "team_unique_shooters"
        and subject in {"home", "away"}
        and intent.get("period", "match") == "match"
    ):
        threshold = _integer(intent.get("threshold"))
        if threshold is not None and threshold >= 1:
            team = home if subject == "home" else away
            return _Recipe(
                key=f"unique_{subject}_shooters_{threshold}_via_team_sot",
                relation_type="team_sot_volume_driver_for_unique_shooters",
                relation_note=(
                    f"{team} reaching {threshold}+ SOT is a team attacking-volume "
                    "driver, not a necessary condition for {threshold}+ distinct "
                    "players attempting a shot: off-target attempts count for the "
                    "target and repeated shooters count for the helper."
                ),
                alpha=0.20,
                helper_question=(
                    f"Will {team} record {threshold} or more shots on target in "
                    "regulation?"
                ),
                helper_intent=_intent(
                    "team_shots_on_target", subject=subject,
                    comparator="gte", threshold=threshold,
                ),
            )

    if (
        market == "none"
        and "any player" in lower
        and "goal" in lower
        and ("or more" in lower or "at least" in lower)
    ):
        threshold = _threshold_at_least(lower)
        if threshold is not None and threshold >= 2:
            return _Recipe(
                key=f"any_player_{threshold}_goals_via_total_{threshold}",
                relation_type="necessary_total_for_any_player_goal_threshold",
                relation_note=(
                    f"A {threshold}+-goal player requires at least {threshold} total "
                    "goals, but team goals can be distributed across players. The "
                    "helper is only a necessary-condition market, not brace odds."
                ),
                alpha=0.25,
                helper_question=(
                    f"Will there be {threshold} or more total goals in regulation?"
                ),
                helper_intent=_intent(
                    "total_goals", comparator="gte", threshold=threshold,
                ),
            )

    if market in {"substitute_score_or_assist", "substitute_score"}:
        return _Recipe(
            key=f"{market}_via_total_goals_3plus",
            relation_type="goal_volume_driver_for_substitute_involvement",
            relation_note=(
                "A higher regulation goal environment creates more substitute goal-"
                "involvement opportunities, but scorer identity and substitution "
                "timing remain in the target simulator."
            ),
            alpha=0.30,
            helper_question="Will there be 3 or more total goals in regulation?",
            helper_intent=_intent(
                "total_goals", comparator="gte", threshold=3,
            ),
        )

    if market == "first_goal_half":
        period = "1H" if intent.get("period") == "1H" else "2H"
        half_name = "first" if period == "1H" else "second"
        return _Recipe(
            key=f"first_goal_{period.lower()}_via_half_goal",
            relation_type="half_goal_super_event_for_first_goal_half",
            relation_note=(
                f"A first goal in the {half_name} half requires a goal in that half, "
                "but the helper does not encode whether an earlier goal occurred."
            ),
            alpha=0.35,
            helper_question=f"Will there be a goal in the {half_name} half?",
            helper_intent=_intent(
                "total_goals", comparator="gte", threshold=1, period=period,
            ),
        )

    if market == "goal_window":
        period, label = _goal_window_helper(lower, intent)
        if period == "match":
            helper_question = "Will there be a goal in regulation?"
            helper_label = "regulation goal"
        else:
            helper_question = f"Will there be a goal in the {label} half?"
            helper_label = f"{label}-half goal"
        return _Recipe(
            key=f"goal_window_via_{period.lower()}_goal",
            relation_type="broader_goal_event_for_goal_window",
            relation_note=(
                f"The {helper_label} market is a broader clock-window event. "
                "The target simulator supplies the within-half timing distribution."
            ),
            alpha=0.30,
            helper_question=helper_question,
            helper_intent=_intent(
                "total_goals", comparator="gte", threshold=1, period=period,
            ),
        )

    if market == "card_stoppage":
        return _Recipe(
            key="card_stoppage_via_total_cards_4plus",
            relation_type="card_volume_driver_for_stoppage_card",
            relation_note=(
                "Full-match card volume is related to stoppage-time card risk but "
                "does not price the required clock window."
            ),
            alpha=0.25,
            helper_question="Will there be 4 or more total cards in regulation?",
            helper_intent=_intent(
                "total_cards", comparator="gte", threshold=4,
            ),
        )

    if market == "lead_any_time" and subject in {"home", "away"}:
        team = home if subject == "home" else away
        return _Recipe(
            key=f"lead_any_time_{subject}_via_first_score",
            relation_type="first_score_component_for_lead_any_time",
            relation_note=(
                "Scoring first normally creates a lead and is the strongest live "
                "component, but a team can lead after conceding and a first goal can "
                "be offset later."
            ),
            alpha=0.55,
            helper_question=f"Will {team} score the first goal in regulation?",
            helper_intent=_intent(
                "first_team_to_score", subject=subject, comparator="yes",
            ),
        )

    if (
        market == "any_team_player_shots_on_target"
        and subject in {"home", "away"}
    ):
        threshold = _integer(intent.get("threshold"))
        if threshold is not None and threshold >= 1:
            team = home if subject == "home" else away
            return _Recipe(
                key=f"any_{subject}_player_sot_{threshold}_via_team_total",
                relation_type="necessary_team_total_for_player_threshold",
                relation_note=(
                    f"A {threshold}+-SOT player requires {team} to record at least "
                    f"{threshold} team SOT, but the team total can be split across "
                    "players."
                ),
                alpha=0.35,
                helper_question=(
                    f"Will {team} record {threshold} or more shots on target in "
                    "regulation?"
                ),
                helper_intent=_intent(
                    "team_shots_on_target", subject=subject,
                    comparator="gte", threshold=threshold,
                ),
            )

    return None


def _goal_window_helper(lower: str, intent: Mapping) -> tuple[str, str]:
    if (
        "either half" in lower
        or "first or second half" in lower
        or "first- or second-half" in lower
        or "first/second half" in lower
    ):
        return "match", "regulation"
    if (
        "before the second hydration" in lower
        or "before second hydration" in lower
    ):
        # This window spans all of the first half and part of the second, so a
        # single-half quote is not a super-event. Regulation total-goal odds are
        # the smallest standard catalogue contract that contains the window.
        return "match", "regulation"
    if "second hydration" in lower or "second-half" in lower:
        return "2H", "second"
    if intent.get("period") == "2H":
        return "2H", "second"
    return "1H", "first"


def _intent(
    market: str,
    *,
    subject: str = "match",
    player: str | None = None,
    comparator: str = "yes",
    threshold: int | None = None,
    period: str = "match",
    time_scope: str = "regulation",
    excludes_own_goals: bool = False,
) -> dict:
    return {
        "market": market,
        "subject": subject,
        "player": player,
        "comparator": comparator,
        "threshold": threshold,
        "period": period,
        "time_scope": time_scope,
        "excludes_own_goals": excludes_own_goals,
    }


def _threshold_at_least(text: str) -> int | None:
    patterns = (
        r"\b(\d+)\s+or more\b",
        r"\bat least\s+(\d+)\b",
        r"\b(\d+)\+",
    )
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return int(match.group(1))
    return None


def _integer(value) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(numeric) or not numeric.is_integer():
        return None
    return int(numeric)


def _first_compared_player_subject(
    question: str,
    intent: Mapping,
    home: str,
    away: str,
) -> str | None:
    """Resolve the first compared player's team only from raw parenthetical text.

    Player names alone are deliberately insufficient: club affiliation, national
    reputation, or a lineup lookup would make proxy selection dependent on hidden
    inference.  The target wording must carry a parenthetical such as
    ``Lamine Yamal (Spain, #19)`` which maps unambiguously to one match side.
    """
    players = str(intent.get("player") or "").split(" vs ", 1)
    if len(players) != 2 or not players[0].strip():
        return None
    match = re.search(
        rf"{re.escape(players[0].strip())}\s*\(([^()]*)\)",
        str(question or ""),
        re.IGNORECASE,
    )
    if not match:
        return None
    team_label = re.sub(
        r"(?:,\s*)?#?\d+\b", "", match.group(1), flags=re.IGNORECASE,
    ).strip(" ,")
    is_home = same_team(team_label, home)
    is_away = same_team(team_label, away)
    if is_home == is_away:
        return None
    return "home" if is_home else "away"


def _helper_id(target_market_id: str, recipe_key: str) -> str:
    return f"{HELPER_ID_PREFIX}:{target_market_id}:{recipe_key}"


def _target_probability(
    estimate: Mapping | None,
) -> tuple[float | None, str | None]:
    if not isinstance(estimate, Mapping):
        return None, None
    baseline = estimate.get("calibrated_baseline")
    if isinstance(baseline, Mapping):
        probability = _pct_probability(baseline.get("probability_pct"))
        if probability is not None:
            source = str(baseline.get("source") or "selected")
            return probability, f"calibrated_baseline:{source}"
    probability = _estimate_probability(estimate)
    return probability, "simulator_estimate" if probability is not None else None


def _estimate_probability(estimate: Mapping | None) -> float | None:
    if not isinstance(estimate, Mapping):
        return None
    probability = _pct_probability(estimate.get("probability_pct"))
    if probability is not None:
        return probability
    return _unit_probability(estimate.get("probability"))


def _observation_probability(observation: Mapping) -> float | None:
    probability = _unit_probability(observation.get("probability"))
    if probability is not None:
        return probability
    return _pct_probability(observation.get("probability_pct"))


def _unit_probability(value) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        probability = float(value)
    except (TypeError, ValueError):
        return None
    if math.isfinite(probability) and 0.0 < probability < 1.0:
        return probability
    return None


def _pct_probability(value) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        probability = float(value) / 100.0
    except (TypeError, ValueError):
        return None
    if math.isfinite(probability) and 0.0 < probability < 1.0:
        return probability
    return None


def _logit(probability: float) -> float:
    bounded = min(1.0 - _EPSILON, max(_EPSILON, probability))
    return math.log(bounded / (1.0 - bounded))


def _logistic(value: float) -> float:
    if value >= 0:
        inverse = math.exp(-value)
        return 1.0 / (1.0 + inverse)
    exponential = math.exp(value)
    return exponential / (1.0 + exponential)
