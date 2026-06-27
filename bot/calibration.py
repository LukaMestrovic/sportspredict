"""Pure outcome-based probability calibration.

The calibrator consumes only frozen raw probabilities, question metadata, raw
pricing cohorts, and explicit binary outcomes.  It never fetches odds, invokes
an LLM, or reads crowd predictions.
"""
from __future__ import annotations

import hashlib
import json
import math
import random
from dataclasses import dataclass
from datetime import datetime, timezone
from itertools import groupby
from typing import Iterable


CALIBRATION_VERSION = "cal1-hierarchical-platt"
FAMILY_VERSION = "cf1"

MIN_BETA = 0.05
MAX_BETA = 5.0
WARMUP_MATCHES = 20
MIN_EVALUATED_MATCHES = 30
MIN_COHORT_OBSERVATIONS = 40
MIN_COHORT_MATCHES = 5
MIN_FAMILY_OBSERVATIONS = 20
MIN_FAMILY_MATCHES = 10
MIN_FAMILY_CLASS = 5
BOOTSTRAP_SAMPLES = 5_000

# Penalized negative log likelihood:
#   .5 * (a^2 + 4(beta - 1)^2 + 8 sum(u_f^2) + 8 sum(v_c^2))
_INTERCEPT_PENALTY = 1.0
_SLOPE_PENALTY = 4.0
_GROUP_PENALTY = 8.0


@dataclass(frozen=True)
class CalibrationObservation:
    lobby_id: str
    match_id: str
    kickoff: str
    market_id: str
    question: str
    raw_probability_int: int
    outcome: int
    family: str
    cohort: str
    official_probability_int: int | None = None
    source_run_id: str | None = None
    provenance: str = "ledger"

    @property
    def raw_probability(self) -> float:
        return max(1, min(99, int(self.raw_probability_int))) / 100.0


@dataclass(frozen=True)
class PrequentialPrediction:
    match_id: str
    kickoff: str
    market_id: str
    family: str
    cohort: str
    outcome: int
    raw_probability_int: int
    calibrated_probability_int: int

    @property
    def raw_brier(self) -> float:
        return (self.raw_probability_int / 100.0 - self.outcome) ** 2

    @property
    def calibrated_brier(self) -> float:
        return (self.calibrated_probability_int / 100.0 - self.outcome) ** 2


@dataclass(frozen=True)
class PlattModel:
    intercept: float
    slope: float
    family_offsets: dict[str, float]
    cohort_offsets: dict[str, float]
    observations: int

    def probability(self, raw_probability: float, family: str, cohort: str) -> float:
        z = _logit(raw_probability)
        eta = (
            self.intercept
            + self.slope * z
            + self.family_offsets.get(family, 0.0)
            + self.cohort_offsets.get(cohort, 0.0)
        )
        return _sigmoid(eta)

    def probability_int(self, raw_probability_int: int, family: str, cohort: str) -> int:
        probability = self.probability(raw_probability_int / 100.0, family, cohort)
        return max(1, min(99, round(probability * 100)))

    def to_dict(self) -> dict:
        return {
            "intercept": self.intercept,
            "slope": self.slope,
            "family_offsets": dict(sorted(self.family_offsets.items())),
            "cohort_offsets": dict(sorted(self.cohort_offsets.items())),
            "observations": self.observations,
        }

    @classmethod
    def from_dict(cls, value: dict) -> "PlattModel":
        return cls(
            intercept=float(value["intercept"]),
            slope=float(value["slope"]),
            family_offsets={k: float(v) for k, v in value["family_offsets"].items()},
            cohort_offsets={k: float(v) for k, v in value["cohort_offsets"].items()},
            observations=int(value["observations"]),
        )


@dataclass(frozen=True)
class CalibrationSnapshot:
    model_id: str
    created_at: str
    observation_hash: str
    model: PlattModel
    global_gate: dict
    family_gates: dict[str, dict]
    cohort_gates: dict[str, dict]
    diagnostics: dict

    def to_dict(self) -> dict:
        return {
            "schema_version": 1,
            "calibration_version": CALIBRATION_VERSION,
            "family_version": FAMILY_VERSION,
            "model_id": self.model_id,
            "created_at": self.created_at,
            "observation_hash": self.observation_hash,
            "model": self.model.to_dict(),
            "global_gate": self.global_gate,
            "family_gates": self.family_gates,
            "cohort_gates": self.cohort_gates,
            "diagnostics": self.diagnostics,
        }

    @classmethod
    def from_dict(cls, value: dict) -> "CalibrationSnapshot":
        if value.get("calibration_version") != CALIBRATION_VERSION:
            raise ValueError("calibration snapshot version mismatch")
        if value.get("family_version") != FAMILY_VERSION:
            raise ValueError("calibration family version mismatch")
        return cls(
            model_id=value["model_id"],
            created_at=value["created_at"],
            observation_hash=value["observation_hash"],
            model=PlattModel.from_dict(value["model"]),
            global_gate=value["global_gate"],
            family_gates=value["family_gates"],
            cohort_gates=value["cohort_gates"],
            diagnostics=value["diagnostics"],
        )

    def apply(
        self,
        raw_probability_int: int,
        family: str,
        cohort: str,
        *,
        enabled: bool = True,
    ) -> tuple[float, int, bool, str]:
        raw_probability_int = max(1, min(99, round(raw_probability_int)))
        raw_probability = raw_probability_int / 100.0
        if not enabled:
            return raw_probability, raw_probability_int, False, "calibration disabled"
        if not self.global_gate.get("active"):
            reason = self.global_gate.get("reason") or "inactive"
            return raw_probability, raw_probability_int, False, f"global gate: {reason}"
        cohort_gate = self.cohort_gates.get(cohort)
        if not cohort_gate or not cohort_gate.get("active"):
            reason = (cohort_gate or {}).get("reason", "cohort not evaluated")
            return raw_probability, raw_probability_int, False, f"cohort gate: {reason}"
        family_gate = self.family_gates.get(family)
        if not family_gate or not family_gate.get("active"):
            reason = (family_gate or {}).get("reason", "family not evaluated")
            return raw_probability, raw_probability_int, False, f"family gate: {reason}"
        calibrated = self.model.probability(raw_probability, family, cohort)
        calibrated_int = max(1, min(99, round(calibrated * 100)))
        return calibrated, calibrated_int, calibrated_int != raw_probability_int, "calibrated"


_MATCH_RESULT_MARKETS = {
    "match_winner", "match_draw", "double_chance", "highest_scoring_half_2h",
    "first_team_to_score",
}
_GOAL_MARKETS = {
    "btts", "total_goals", "team_total_goals", "team_score", "team_score_1h",
    "team_score_2h",
}
_FAMILY_BY_MARKET = {
    **{name: "match_result_timing" for name in _MATCH_RESULT_MARKETS},
    **{name: "goals_team_scoring" for name in _GOAL_MARKETS},
    "total_corners": "corners",
    "team_corners": "corners",
    "corners_compare": "corners",
    "total_cards": "cards",
    "team_cards": "cards",
    "cards_compare": "cards",
    "total_offsides": "offsides",
    "team_offsides": "offsides",
    "offsides_compare": "offsides",
    "total_fouls": "fouls",
    "team_fouls": "fouls",
    "fouls_compare": "fouls",
    "total_shots_on_target": "match_team_shots_on_target",
    "team_shots_on_target": "match_team_shots_on_target",
    "shots_on_target_compare": "match_team_shots_on_target",
    "player_shots_on_target": "player_shots_on_target",
    "player_goal_scorer": "player_goal_involvement",
    "player_score_or_assist": "player_goal_involvement",
}


def family_for(question: str, intent: dict | None = None) -> str:
    """Return a stable calibration family without requiring an LLM parse."""
    lower = question.lower()
    market = (intent or {}).get("market")

    # Special contracts must be recognized before their component keywords.
    if "penalty kick" in lower or "red card be shown" in lower:
        return "penalty_red_card"
    if "both teams" in lower and "shot" in lower and "target" in lower:
        return "both_teams_shots_on_target"
    if "both teams score" in lower and (" and " in lower or " or " in lower):
        return "goal_compound"
    if "first goal of the game and" in lower:
        return "rare_unknown_compound"
    if "score or assist" in lower or market in {
        "player_goal_scorer", "player_score_or_assist",
    }:
        return "player_goal_involvement"
    if market == "player_shots_on_target":
        return "player_shots_on_target"
    if market in _FAMILY_BY_MARKET:
        return _FAMILY_BY_MARKET[market]

    # Legacy backfill can lack a usable intent because provider team aliases
    # differ.  These wording fallbacks are deliberately team-name agnostic.
    if "corner" in lower:
        return "corners"
    if "offside" in lower:
        return "offsides"
    if "foul" in lower:
        return "fouls"
    if "card" in lower or "booked" in lower:
        return "cards"
    if "shot" in lower and "target" in lower:
        return "match_team_shots_on_target"
    if any(term in lower for term in (
        "win the match", "be winning", "match be tied", "tied at halftime",
        "a draw", "more goals than", "more total goals than",
    )):
        return "match_result_timing"
    if "goal" in lower or "score" in lower:
        return "goals_team_scoring"
    return "rare_unknown_compound"


def observation_hash(observations: Iterable[CalibrationObservation]) -> str:
    rows = [
        {
            "lobby_id": row.lobby_id,
            "match_id": row.match_id,
            "kickoff": row.kickoff,
            "market_id": row.market_id,
            "raw_probability_int": row.raw_probability_int,
            "outcome": row.outcome,
            "family": row.family,
            "cohort": row.cohort,
        }
        for row in observations
    ]
    rows.sort(key=lambda row: (row["kickoff"], row["match_id"], row["market_id"]))
    blob = json.dumps(
        {"calibration_version": CALIBRATION_VERSION, "rows": rows},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def fit_model(observations: Iterable[CalibrationObservation]) -> PlattModel:
    rows = sorted(
        observations, key=lambda row: (row.kickoff, row.match_id, row.market_id)
    )
    if not rows:
        return PlattModel(0.0, 1.0, {}, {}, 0)

    families = sorted({row.family for row in rows})
    cohorts = sorted({row.cohort for row in rows})
    family_index = {name: 2 + i for i, name in enumerate(families)}
    cohort_index = {
        name: 2 + len(families) + i for i, name in enumerate(cohorts)
    }
    size = 2 + len(families) + len(cohorts)
    target = [0.0, 1.0] + [0.0] * (size - 2)
    penalty = (
        [_INTERCEPT_PENALTY, _SLOPE_PENALTY]
        + [_GROUP_PENALTY] * (size - 2)
    )
    theta = list(target)

    vectors: list[tuple[list[float], int]] = []
    for row in rows:
        vector = [0.0] * size
        vector[0] = 1.0
        vector[1] = _logit(row.raw_probability)
        vector[family_index[row.family]] = 1.0
        vector[cohort_index[row.cohort]] = 1.0
        vectors.append((vector, row.outcome))

    for _ in range(80):
        gradient = [penalty[i] * (theta[i] - target[i]) for i in range(size)]
        hessian = [[0.0] * size for _ in range(size)]
        for i in range(size):
            hessian[i][i] = penalty[i]
        for vector, outcome in vectors:
            probability = _sigmoid(sum(t * x for t, x in zip(theta, vector)))
            residual = probability - outcome
            weight = max(1e-10, probability * (1.0 - probability))
            nonzero = [i for i, value in enumerate(vector) if value]
            for i in nonzero:
                gradient[i] += residual * vector[i]
                for j in nonzero:
                    hessian[i][j] += weight * vector[i] * vector[j]
        step = _solve(hessian, gradient)
        before = _objective(theta, vectors, target, penalty)
        scale = 1.0
        candidate = theta
        while scale >= 1e-7:
            candidate = [value - scale * delta for value, delta in zip(theta, step)]
            candidate[1] = max(MIN_BETA, min(MAX_BETA, candidate[1]))
            if _objective(candidate, vectors, target, penalty) <= before:
                break
            scale *= 0.5
        movement = max(abs(a - b) for a, b in zip(theta, candidate))
        theta = candidate
        if movement < 1e-9:
            break

    return PlattModel(
        intercept=theta[0],
        slope=theta[1],
        family_offsets={name: theta[index] for name, index in family_index.items()},
        cohort_offsets={name: theta[index] for name, index in cohort_index.items()},
        observations=len(rows),
    )


def prequential_predictions(
    observations: Iterable[CalibrationObservation],
) -> list[PrequentialPrediction]:
    """Replay calibration using only observations from earlier kickoff slots."""
    rows = sorted(
        observations, key=lambda row: (row.kickoff, row.match_id, row.market_id)
    )
    history: list[CalibrationObservation] = []
    predictions: list[PrequentialPrediction] = []
    for _kickoff, slot_iter in groupby(rows, key=lambda row: row.kickoff):
        slot = list(slot_iter)
        prior_matches = len({row.match_id for row in history})
        if prior_matches >= WARMUP_MATCHES:
            model = fit_model(history)
            predictions.extend(
                PrequentialPrediction(
                    match_id=row.match_id,
                    kickoff=row.kickoff,
                    market_id=row.market_id,
                    family=row.family,
                    cohort=row.cohort,
                    outcome=row.outcome,
                    raw_probability_int=row.raw_probability_int,
                    calibrated_probability_int=model.probability_int(
                        row.raw_probability_int, row.family, row.cohort
                    ),
                )
                for row in slot
            )
        history.extend(slot)
    return predictions


def build_snapshot(
    observations: Iterable[CalibrationObservation],
    *,
    created_at: str | None = None,
    bootstrap_samples: int = BOOTSTRAP_SAMPLES,
) -> CalibrationSnapshot:
    rows = list(observations)
    obs_hash = observation_hash(rows)
    model = fit_model(rows)
    replay = prequential_predictions(rows)
    global_gate = _global_gate(replay, obs_hash, bootstrap_samples)
    family_gates = {
        family: _family_gate([row for row in replay if row.family == family])
        for family in sorted({row.family for row in rows})
    }
    cohort_gates = {
        cohort: _cohort_gate([row for row in replay if row.cohort == cohort])
        for cohort in sorted({row.cohort for row in rows})
    }
    diagnostics = _diagnostics(replay)
    identity = {
        "calibration_version": CALIBRATION_VERSION,
        "family_version": FAMILY_VERSION,
        "observation_hash": obs_hash,
        "model": model.to_dict(),
        "global_gate": global_gate,
        "family_gates": family_gates,
        "cohort_gates": cohort_gates,
    }
    model_id = hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:20]
    return CalibrationSnapshot(
        model_id=model_id,
        created_at=created_at or datetime.now(timezone.utc).isoformat(timespec="seconds"),
        observation_hash=obs_hash,
        model=model,
        global_gate=global_gate,
        family_gates=family_gates,
        cohort_gates=cohort_gates,
        diagnostics=diagnostics,
    )


def _global_gate(
    rows: list[PrequentialPrediction], observation_digest: str, bootstrap_samples: int
) -> dict:
    matches = len({row.match_id for row in rows})
    if matches < MIN_EVALUATED_MATCHES:
        return {
            "active": False,
            "reason": f"need {MIN_EVALUATED_MATCHES} evaluated matches; have {matches}",
            "matches": matches,
            "observations": len(rows),
        }
    delta = _mean_delta(rows)
    upper = _bootstrap_upper(rows, observation_digest, bootstrap_samples)
    active = delta < 0.0 and upper < 0.0
    reason = "validated improvement" if active else (
        f"no validated improvement (delta={delta:+.6f}, upper90={upper:+.6f})"
    )
    return {
        "active": active,
        "reason": reason,
        "matches": matches,
        "observations": len(rows),
        "mean_brier_delta": delta,
        "bootstrap_upper_90": upper,
    }


def _family_gate(rows: list[PrequentialPrediction]) -> dict:
    matches = len({row.match_id for row in rows})
    zeroes = sum(row.outcome == 0 for row in rows)
    ones = len(rows) - zeroes
    reasons = []
    if len(rows) < MIN_FAMILY_OBSERVATIONS:
        reasons.append(f"need {MIN_FAMILY_OBSERVATIONS} observations; have {len(rows)}")
    if matches < MIN_FAMILY_MATCHES:
        reasons.append(f"need {MIN_FAMILY_MATCHES} matches; have {matches}")
    if min(zeroes, ones) < MIN_FAMILY_CLASS:
        reasons.append(
            f"need {MIN_FAMILY_CLASS} of each outcome; have zero={zeroes}, one={ones}"
        )
    delta = _mean_delta(rows) if rows else None
    if delta is not None and delta > 0.0:
        reasons.append(f"prequential Brier worsened by {delta:.6f}")
    return {
        "active": not reasons,
        "reason": "; ".join(reasons) if reasons else "prequential Brier non-worsening",
        "matches": matches,
        "observations": len(rows),
        "zeroes": zeroes,
        "ones": ones,
        "mean_brier_delta": delta,
    }


def _cohort_gate(rows: list[PrequentialPrediction]) -> dict:
    matches = len({row.match_id for row in rows})
    reasons = []
    if len(rows) < MIN_COHORT_OBSERVATIONS:
        reasons.append(f"need {MIN_COHORT_OBSERVATIONS} observations; have {len(rows)}")
    if matches < MIN_COHORT_MATCHES:
        reasons.append(f"need {MIN_COHORT_MATCHES} matches; have {matches}")
    delta = _mean_delta(rows) if rows else None
    if delta is not None and delta > 0.0:
        reasons.append(f"prequential Brier worsened by {delta:.6f}")
    return {
        "active": not reasons,
        "reason": "; ".join(reasons) if reasons else "prequential Brier non-worsening",
        "matches": matches,
        "observations": len(rows),
        "mean_brier_delta": delta,
    }


def _diagnostics(rows: list[PrequentialPrediction]) -> dict:
    if not rows:
        return {
            "prequential_observations": 0,
            "prequential_matches": 0,
            "raw_mean_brier": None,
            "calibrated_mean_brier": None,
            "mean_brier_delta": None,
            "raw_reliability": [],
            "calibrated_reliability": [],
        }
    raw = [row.raw_probability_int / 100.0 for row in rows]
    calibrated = [row.calibrated_probability_int / 100.0 for row in rows]
    outcomes = [row.outcome for row in rows]
    return {
        "prequential_observations": len(rows),
        "prequential_matches": len({row.match_id for row in rows}),
        "raw_mean_brier": sum(row.raw_brier for row in rows) / len(rows),
        "calibrated_mean_brier": (
            sum(row.calibrated_brier for row in rows) / len(rows)
        ),
        "mean_brier_delta": _mean_delta(rows),
        "raw_calibration_line": _calibration_line(raw, outcomes),
        "calibrated_calibration_line": _calibration_line(calibrated, outcomes),
        "raw_reliability": _reliability(raw, outcomes),
        "calibrated_reliability": _reliability(calibrated, outcomes),
    }


def _mean_delta(rows: list[PrequentialPrediction]) -> float:
    return sum(row.calibrated_brier - row.raw_brier for row in rows) / len(rows)


def _bootstrap_upper(
    rows: list[PrequentialPrediction], digest: str, samples: int
) -> float:
    blocks: dict[str, list[float]] = {}
    for row in rows:
        blocks.setdefault(row.match_id, []).append(row.calibrated_brier - row.raw_brier)
    ordered = [blocks[key] for key in sorted(blocks)]
    if not ordered or samples <= 0:
        return float("inf")
    rng = random.Random(int(digest[:16], 16))
    means = []
    for _ in range(samples):
        chosen = [ordered[rng.randrange(len(ordered))] for _ in ordered]
        values = [value for block in chosen for value in block]
        means.append(sum(values) / len(values))
    means.sort()
    return means[max(0, math.ceil(0.90 * len(means)) - 1)]


def _reliability(probabilities: list[float], outcomes: list[int]) -> list[dict]:
    bins: list[list[tuple[float, int]]] = [[] for _ in range(10)]
    for probability, outcome in zip(probabilities, outcomes):
        index = min(9, int(max(0.0, min(0.999999, probability)) * 10))
        bins[index].append((probability, outcome))
    return [
        {
            "lower": index / 10.0,
            "upper": (index + 1) / 10.0,
            "observations": len(values),
            "mean_probability": sum(p for p, _ in values) / len(values),
            "outcome_rate": sum(y for _, y in values) / len(values),
        }
        for index, values in enumerate(bins)
        if values
    ]


def _calibration_line(probabilities: list[float], outcomes: list[int]) -> dict:
    if len(probabilities) < 2 or len(set(outcomes)) < 2:
        return {"intercept": None, "slope": None}
    theta = [0.0, 1.0]
    for _ in range(60):
        gradient = [1e-8 * theta[0], 1e-8 * (theta[1] - 1.0)]
        hessian = [[1e-8, 0.0], [0.0, 1e-8]]
        for probability, outcome in zip(probabilities, outcomes):
            vector = [1.0, _logit(probability)]
            fitted = _sigmoid(theta[0] + theta[1] * vector[1])
            residual = fitted - outcome
            weight = max(1e-10, fitted * (1.0 - fitted))
            for i in range(2):
                gradient[i] += residual * vector[i]
                for j in range(2):
                    hessian[i][j] += weight * vector[i] * vector[j]
        step = _solve(hessian, gradient)
        candidate = [theta[i] - step[i] for i in range(2)]
        if max(abs(candidate[i] - theta[i]) for i in range(2)) < 1e-9:
            theta = candidate
            break
        theta = candidate
    return {"intercept": theta[0], "slope": theta[1]}


def _objective(theta, vectors, target, penalty) -> float:
    total = 0.5 * sum(
        weight * (value - center) ** 2
        for value, center, weight in zip(theta, target, penalty)
    )
    for vector, outcome in vectors:
        eta = sum(t * x for t, x in zip(theta, vector))
        total += max(eta, 0.0) - outcome * eta + math.log1p(math.exp(-abs(eta)))
    return total


def _solve(matrix: list[list[float]], values: list[float]) -> list[float]:
    """Solve a small dense linear system using pivoted Gaussian elimination."""
    size = len(values)
    augmented = [list(row) + [values[i]] for i, row in enumerate(matrix)]
    for column in range(size):
        pivot = max(range(column, size), key=lambda row: abs(augmented[row][column]))
        if abs(augmented[pivot][column]) < 1e-14:
            raise ValueError("singular calibration Hessian")
        augmented[column], augmented[pivot] = augmented[pivot], augmented[column]
        divisor = augmented[column][column]
        for j in range(column, size + 1):
            augmented[column][j] /= divisor
        for row in range(size):
            if row == column:
                continue
            factor = augmented[row][column]
            if not factor:
                continue
            for j in range(column, size + 1):
                augmented[row][j] -= factor * augmented[column][j]
    return [augmented[row][size] for row in range(size)]


def _logit(probability: float) -> float:
    probability = max(1e-6, min(1.0 - 1e-6, float(probability)))
    return math.log(probability / (1.0 - probability))


def _sigmoid(value: float) -> float:
    if value >= 0:
        return 1.0 / (1.0 + math.exp(-value))
    exp_value = math.exp(value)
    return exp_value / (1.0 + exp_value)
