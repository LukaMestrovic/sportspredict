"""Configuration loading.

Reads ``config/settings.yaml`` (baseline parameters + run settings) and
``config/market_rules.yaml`` (per-market resolution rules). A single
:class:`Settings` object is threaded through the rate model, simulator and
market layer so behaviour stays config-driven and reproducible.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml


def _project_root() -> Path:
    """Locate the repo root (the directory containing ``config/``)."""
    # Allow override for tests / installed use.
    env = os.environ.get("SPORTSPREDICT_ROOT")
    if env:
        return Path(env)
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "config" / "settings.yaml").exists():
            return parent
    # Fall back to two levels up from src/sportspredict/.
    return here.parents[2]


@dataclass(frozen=True)
class Settings:
    """Parsed configuration. ``raw`` keeps the full settings dict for niche access."""

    raw: dict[str, Any]
    market_rules: dict[str, Any]
    root: Path

    # --- convenience accessors over the most-used blocks -------------------
    @property
    def n_sims(self) -> int:
        return int(self.raw["simulation"]["n_sims"])

    @property
    def seed(self) -> int:
        return int(self.raw["simulation"]["seed"])

    @property
    def baseline_rates(self) -> dict[str, float]:
        return self.raw["baseline_rates"]

    @property
    def half_share_h1(self) -> dict[str, float]:
        return self.raw["half_share_h1"]

    @property
    def strength_coeffs(self) -> dict[str, float]:
        return self.raw["strength_coeffs"]

    @property
    def context_effects(self) -> dict[str, float]:
        return self.raw["context_effects"]

    @property
    def dispersion(self) -> dict[str, Any]:
        return self.raw["dispersion"]

    @property
    def goals_model(self) -> dict[str, float]:
        return self.raw["goals_model"]

    @property
    def players(self) -> dict[str, Any]:
        return self.raw["players"]

    @property
    def markets(self) -> dict[str, Any]:
        return self.raw["markets"]

    def path(self, rel: str) -> Path:
        """Resolve a config-relative path against the project root."""
        p = Path(rel)
        return p if p.is_absolute() else self.root / p


def load_settings(
    settings_path: str | Path | None = None,
    market_rules_path: str | Path | None = None,
) -> Settings:
    """Load settings + market rules. Paths default to ``config/`` under the root."""
    root = _project_root()
    sp = Path(settings_path) if settings_path else root / "config" / "settings.yaml"
    mp = (
        Path(market_rules_path)
        if market_rules_path
        else root / "config" / "market_rules.yaml"
    )
    with open(sp) as fh:
        raw = yaml.safe_load(fh)
    with open(mp) as fh:
        rules = yaml.safe_load(fh)
    return Settings(raw=raw, market_rules=rules, root=root)


@lru_cache(maxsize=1)
def default_settings() -> Settings:
    """Process-wide cached default settings."""
    return load_settings()
