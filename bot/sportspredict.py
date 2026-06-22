"""Thin client for the SportPredict Probability Cup REST API."""
from __future__ import annotations

import time
from typing import Any

import requests

from . import config

EVENT_TITLE = "Jump Trading Probability Cup"


class SportPredict:
    def __init__(self, key: str | None = None):
        self.key = key or config.SPORTSPREDICT_KEY
        self.s = requests.Session()
        self.s.headers["Authorization"] = f"Bearer {self.key}"

    def _get(self, path: str, **params) -> Any:
        for attempt in range(4):
            r = self.s.get(f"{config.SP_BASE}{path}", params=params, timeout=30)
            if r.status_code == 429:  # rate limited: 60/min
                time.sleep(2 ** attempt)
                continue
            r.raise_for_status()
            return r.json()
        r.raise_for_status()

    def _post(self, path: str, body: dict) -> Any:
        r = self.s.post(f"{config.SP_BASE}{path}", json=body, timeout=30)
        r.raise_for_status()
        return r.json()

    # --- discovery ---
    def event(self) -> dict:
        events = self._get("/events", limit=20)
        for e in events:
            if e.get("title") == EVENT_TITLE:
                return e
        return events[0]

    def lobby(self, event_id: str) -> dict:
        lobbies = self._get("/lobbies", event_id=event_id)
        lob = lobbies[0]
        if not lob.get("joined"):
            try:
                self._post(f"/lobbies/{lob['id']}/join", {})
            except requests.HTTPError:
                pass  # 409 = already joined
        return lob

    def matches(self, event_id: str, lobby_id: str) -> list[dict]:
        return self._get("/matches", event_id=event_id, lobby_id=lobby_id)

    def markets(self, lobby_id: str, match_id: str) -> list[dict]:
        return self._get("/markets", lobby_id=lobby_id, match_id=match_id)

    # --- predictions ---
    def submit_batch(self, predictions: list[dict]) -> dict:
        """predictions: [{market_id, lobby_id, probability(1-99 int)}]"""
        return self._post("/predictions/batch", {"predictions": predictions})

    def results(self, lobby_id: str) -> list[dict]:
        """Settled predictions (closed + resolved markets) with brier scores."""
        return self._get("/results", lobby_id=lobby_id)
