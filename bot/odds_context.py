"""Shared immutable inputs for deterministic odds and evidence collection."""

from __future__ import annotations

from dataclasses import dataclass

from .oddsapi import OddsAPI


@dataclass
class PriceCtx:
    """Provider snapshots and match identity used while building evidence."""

    home: str
    away: str
    af_books: list
    oa: OddsAPI | None
    oa_event: dict | None
    stage: str | None = None
