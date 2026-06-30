"""Learned event-time distributions used by additive post-simulation markets.

The team-rate simulator owns *how many* events occur.  This module supplies the missing
within-half clock: empirical minute/added-time distributions fitted from cached historical events.
Missing artifacts or event families fall back to neutral uniform timing and never break pricing.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np

PHASES = ("1H", "2H", "ET")


@dataclass(frozen=True)
class SampledTimes:
    phase: np.ndarray
    minute: np.ndarray
    extra: np.ndarray
    order: np.ndarray


def _fallback_tokens(phase: str) -> dict[str, float]:
    if phase == "1H":
        return {f"{m}|0": 1.0 for m in range(1, 46)}
    if phase == "2H":
        return {f"{m}|0": 1.0 for m in range(46, 91)}
    return {f"{m}|0": 1.0 for m in range(91, 121)}


def _order_value(phase: str, minute: float, extra: float) -> float:
    # Added time belongs before the following phase even when display minutes overlap (45+2 vs 46').
    if phase == "1H" and extra > 0:
        return 45.0 + min(extra, 99.0) / 100.0
    if phase == "2H" and extra > 0:
        return 90.0 + min(extra, 99.0) / 100.0
    if phase == "ET":
        return 100.0 + minute + min(extra, 99.0) / 100.0
    return minute


class TimingModel:
    """Small JSON-backed collection of empirical categorical timing distributions."""

    def __init__(self, data: dict | None = None):
        self.data = data or {}

    @classmethod
    def load(cls, path: str | Path | None) -> "TimingModel":
        if not path or not Path(path).exists():
            return cls()
        try:
            return cls(json.loads(Path(path).read_text()))
        except Exception:
            return cls()

    def has(self, event_type: str) -> bool:
        return bool((self.data.get("event_types") or {}).get(event_type))

    def sample(
        self, event_type: str, phase: str, size: int, rng: np.random.Generator
    ) -> SampledTimes:
        if size <= 0:
            zf = np.empty(0, dtype=float)
            return SampledTimes(np.empty(0, dtype="U2"), zf, zf, zf)
        raw = (((self.data.get("event_types") or {}).get(event_type) or {}).get(phase) or {})
        tokens = raw.get("tokens") or _fallback_tokens(phase)
        labels = list(tokens)
        weights = np.asarray([max(float(tokens[k]), 0.0) for k in labels], dtype=float)
        if not np.isfinite(weights).all() or weights.sum() <= 0:
            labels = list(_fallback_tokens(phase))
            weights = np.ones(len(labels), dtype=float)
        chosen = rng.choice(len(labels), size=size, p=weights / weights.sum())
        minute = np.empty(size, dtype=float)
        extra = np.empty(size, dtype=float)
        for i, idx in enumerate(chosen):
            m, e = labels[int(idx)].split("|", 1)
            minute[i], extra[i] = float(m), float(e)
        order = np.asarray([_order_value(phase, m, e) for m, e in zip(minute, extra)])
        order += rng.random(size) * 1e-4  # preserve order while breaking same-recorded-minute ties
        return SampledTimes(np.full(size, phase), minute, extra, order)

    def sample_any_phase(
        self, event_type: str, size: int, rng: np.random.Generator
    ) -> SampledTimes:
        return self.sample_phases(event_type, PHASES, size, rng)

    def sample_phases(
        self, event_type: str, phases: tuple[str, ...], size: int, rng: np.random.Generator
    ) -> SampledTimes:
        """Sample an event clock conditional on the phases the simulated world actually plays."""
        raw = (self.data.get("event_types") or {}).get(event_type) or {}
        phase_weights = np.asarray([
            sum(float(v) for v in ((raw.get(p) or {}).get("tokens") or {}).values())
            for p in phases
        ])
        if phase_weights.sum() <= 0:
            phase_weights = np.ones(len(phases), dtype=float)
        picked = rng.choice(len(phases), size=size, p=phase_weights / phase_weights.sum())
        phase = np.empty(size, dtype="U2")
        minute = np.empty(size, dtype=float)
        extra = np.empty(size, dtype=float)
        order = np.empty(size, dtype=float)
        for j, p in enumerate(phases):
            idx = np.flatnonzero(picked == j)
            sampled = self.sample(event_type, p, len(idx), rng)
            phase[idx], minute[idx], extra[idx], order[idx] = (
                sampled.phase, sampled.minute, sampled.extra, sampled.order
            )
        return SampledTimes(phase, minute, extra, order)

    def rate(self, name: str, stage: str | None = None, default: float = 0.0) -> float:
        rates = (self.data.get("binary_rates") or {}).get(name) or {}
        value = rates.get(stage or "") if stage else None
        if value is None:
            value = rates.get("all", default)
        try:
            return float(np.clip(float(value), 0.0, 1.0))
        except (TypeError, ValueError):
            return float(default)

    def parameter(self, name: str, default: float) -> float:
        try:
            return float((self.data.get("parameters") or {}).get(name, default))
        except (TypeError, ValueError):
            return float(default)
