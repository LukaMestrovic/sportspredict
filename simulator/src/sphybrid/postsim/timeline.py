"""Post-simulation event timelines learned from historical event timestamps."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from sportspredict.model.outcome import MatchOutcome
from sportspredict.types import GOALS, H1, H2, TEAM_A, TEAM_B

from .timing import TimingModel

_PHASE_IDX = {"1H": H1, "2H": H2, "ET": 2}


@dataclass
class EventTimeline:
    n_sims: int
    world: np.ndarray
    team: np.ndarray
    phase: np.ndarray
    minute: np.ndarray
    extra: np.ndarray
    order: np.ndarray

    def select(
        self, *, after: float | None = None, through: float | None = None,
        phases: set[str] | None = None, stoppage: str | None = None,
    ) -> np.ndarray:
        mask = np.ones(self.minute.size, dtype=bool)
        if after is not None:
            mask &= self.minute > float(after)
        if through is not None:
            mask &= self.minute <= float(through)
        if phases:
            mask &= np.isin(self.phase, list(phases))
        if stoppage:
            mask &= (self.phase == stoppage) & (self.extra > 0)
        return mask

    def counts(self, mask: np.ndarray) -> np.ndarray:
        return np.bincount(self.world[mask], minlength=self.n_sims)

    def any(self, mask: np.ndarray) -> np.ndarray:
        return self.counts(mask) >= 1


def _empty(n: int) -> EventTimeline:
    return EventTimeline(
        n, np.empty(0, int), np.empty(0, np.int8), np.empty(0, "U2"),
        np.empty(0, float), np.empty(0, float), np.empty(0, float),
    )


def _emit(
    buckets: list[tuple], counts: np.ndarray, team: int, event_type: str, phase: str,
    timing: TimingModel, rng: np.random.Generator,
) -> None:
    total = int(np.asarray(counts).sum())
    if total <= 0:
        return
    world = np.repeat(np.arange(len(counts), dtype=np.int64), np.asarray(counts, dtype=int))
    sampled = timing.sample(event_type, phase, total, rng)
    buckets.append((world, np.full(total, team, np.int8), sampled.phase,
                    sampled.minute, sampled.extra, sampled.order))


def _assemble(n: int, buckets: list[tuple]) -> EventTimeline:
    if not buckets:
        return _empty(n)
    cols = [np.concatenate([b[i] for b in buckets]) for i in range(6)]
    return EventTimeline(n, *cols)


def count_timeline(
    outcome: MatchOutcome, stat: str, event_type: str, timing: TimingModel,
    rng: np.random.Generator,
) -> EventTimeline:
    buckets: list[tuple] = []
    for team in (TEAM_A, TEAM_B):
        _emit(buckets, outcome.reg_counts[stat][team, H1], team, event_type, "1H", timing, rng)
        _emit(buckets, outcome.reg_counts[stat][team, H2], team, event_type, "2H", timing, rng)
        if stat in outcome.et_counts:
            _emit(buckets, outcome.et_counts[stat][team], team, event_type, "ET", timing, rng)
    return _assemble(outcome.n_sims, buckets)


def card_timeline(
    outcome: MatchOutcome, timing: TimingModel, rng: np.random.Generator,
    *, et_scale: float = 0.30, red: EventTimeline | None = None,
) -> EventTimeline:
    base = count_timeline(outcome, "yellows", "yellow_cards", timing, rng)
    buckets = [(base.world, base.team, base.phase, base.minute, base.extra, base.order)] \
        if base.world.size else []
    red = red or red_card_timeline(outcome, timing, rng, et_scale=et_scale)
    if red.world.size:
        buckets.append((red.world, red.team, red.phase, red.minute, red.extra, red.order))
    return _assemble(outcome.n_sims, buckets)


def _rare_timeline(
    outcome: MatchOutcome, totals: np.ndarray, event_type: str,
    timing: TimingModel, rng: np.random.Generator, *, et_scale: float,
) -> EventTimeline:
    """Recover regulation/ET Poisson components after the wheel stored only their sum.

    The wheel samples independent ``Pois(mu)`` regulation and ``Pois(mu*et_scale)`` ET counts.
    Conditional on their stored sum, the ET component is exactly binomial with probability
    ``et_scale / (1 + et_scale)`` in worlds that reached extra time.
    """
    values = np.asarray(totals, dtype=int)
    if values.ndim == 1:
        values = values[None, :]
    p_et = float(et_scale) / (1.0 + float(et_scale)) if et_scale > 0 else 0.0
    buckets: list[tuple] = []
    for row, counts in enumerate(values):
        team = row if values.shape[0] > 1 else -1
        et_counts = np.zeros_like(counts)
        played = np.asarray(outcome.et_played, dtype=bool)
        et_counts[played] = rng.binomial(counts[played], p_et)
        reg_counts = counts - et_counts

        reg_world = np.repeat(np.arange(outcome.n_sims), reg_counts)
        if reg_world.size:
            sampled = timing.sample_phases(event_type, ("1H", "2H"), len(reg_world), rng)
            buckets.append((reg_world, np.full(len(reg_world), team, np.int8), sampled.phase,
                            sampled.minute, sampled.extra, sampled.order))
        et_world = np.repeat(np.arange(outcome.n_sims), et_counts)
        if et_world.size:
            sampled = timing.sample(event_type, "ET", len(et_world), rng)
            buckets.append((et_world, np.full(len(et_world), team, np.int8), sampled.phase,
                            sampled.minute, sampled.extra, sampled.order))
    return _assemble(outcome.n_sims, buckets)


def red_card_timeline(
    outcome: MatchOutcome, timing: TimingModel, rng: np.random.Generator,
    *, et_scale: float = 0.30,
) -> EventTimeline:
    return _rare_timeline(
        outcome, outcome.reds, "red_cards", timing, rng, et_scale=et_scale,
    )


def penalty_timeline(
    outcome: MatchOutcome, timing: TimingModel, rng: np.random.Generator,
    *, et_scale: float = 0.30,
) -> EventTimeline:
    return _rare_timeline(
        outcome, outcome.penalties, "penalties", timing, rng, et_scale=et_scale,
    )


class GoalTimeline(EventTimeline):
    """Goal events plus first-goal order by team and phase."""

    def __init__(self, base: EventTimeline):
        super().__init__(**base.__dict__)
        self.first_order = np.full((2, 3, self.n_sims), np.inf)
        for team in (TEAM_A, TEAM_B):
            for phase, pidx in _PHASE_IDX.items():
                mask = (self.team == team) & (self.phase == phase)
                if mask.any():
                    np.minimum.at(self.first_order[team, pidx], self.world[mask], self.order[mask])
        # Backward-compatible aliases used by validation/tests.
        self.g_world, self.g_team, self.g_minute = self.world, self.team, self.minute
        self.g_phase, self.g_extra = self.phase, self.extra
        self.first_min = np.minimum(self.first_order[:, H1], self.first_order[:, H2])

    @classmethod
    def from_outcome(
        cls, outcome: MatchOutcome, rng: np.random.Generator, et_minutes: float = 30.0,
        timing: TimingModel | None = None,
    ) -> "GoalTimeline":
        del et_minutes  # retained for backward API compatibility; learned ET tokens own the clock.
        return cls(count_timeline(outcome, GOALS, "goals", timing or TimingModel(), rng))

    def first_scorer_is(self, team: int, half: str | None = None) -> np.ndarray:
        other = TEAM_B if team == TEAM_A else TEAM_A
        if half in ("1H", "2H", "ET"):
            idx = _PHASE_IDX[half]
            return self.first_order[team, idx] < self.first_order[other, idx]
        mine = np.minimum(self.first_order[team, H1], self.first_order[team, H2])
        theirs = np.minimum(self.first_order[other, H1], self.first_order[other, H2])
        return mine < theirs

    def any_first_goal(self) -> np.ndarray:
        return np.isfinite(self.first_order[:, :2]).any(axis=(0, 1))

    def any_goal_in_window(
        self, lo: float, hi: float, team: int | None = None,
        phases: set[str] | None = None,
    ) -> np.ndarray:
        mask = self.select(after=lo, through=hi, phases=phases)
        if team is not None:
            mask &= self.team == team
        return self.any(mask)
