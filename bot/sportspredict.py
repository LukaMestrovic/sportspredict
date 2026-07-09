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

    def _patch(self, path: str, body: dict) -> Any:
        r = self.s.patch(f"{config.SP_BASE}{path}", json=body, timeout=30)
        r.raise_for_status()
        return r.json()

    # --- discovery ---
    def event(self) -> dict:
        events = self._get("/events", limit=20)
        for e in events:
            if e.get("title") == EVENT_TITLE:
                return e
        available = ", ".join(
            f"{event.get('title', '<untitled>')} ({event.get('id', '?')})"
            for event in events
        ) or "none"
        raise LookupError(
            f"required SportPredict event {EVENT_TITLE!r} not found; "
            f"available events: {available}"
        )

    def lobby(self, event_id: str) -> dict:
        lobbies = self._get("/lobbies", event_id=event_id)
        if not lobbies:
            raise LookupError(f"no SportPredict lobby found for event {event_id}")
        lob = lobbies[0]
        if not lob.get("joined"):
            try:
                self._post(f"/lobbies/{lob['id']}/join", {})
            except requests.HTTPError as exc:
                if getattr(exc.response, "status_code", None) != 409:
                    raise
        return lob

    def matches(self, event_id: str, lobby_id: str) -> list[dict]:
        return self._get("/matches", event_id=event_id, lobby_id=lobby_id)

    def markets(self, lobby_id: str, match_id: str) -> list[dict]:
        return self._get("/markets", lobby_id=lobby_id, match_id=match_id)

    # --- predictions ---
    def submit_batch(self, predictions: list[dict]) -> dict:
        """Create up to 50 new predictions: [{market_id, lobby_id, probability}].

        The API allows only ONE prediction per market: re-POSTing an existing
        market is rejected per-item ("already exists") in the returned
        {succeeded, failed, results} body — it does not raise. To CHANGE an
        existing prediction use ``update_prediction`` (see ``upsert``).
        """
        return self._post("/predictions/batch", {"predictions": predictions})

    def list_predictions(self, lobby_id: str) -> list[dict]:
        """Every prediction this account holds in the lobby (open/closed/settled),
        each with its stable prediction ``id`` and ``market_id``."""
        return self._get("/predictions", lobby_id=lobby_id)

    def update_prediction(self, prediction_id: str, probability: int) -> dict:
        """PATCH an existing prediction's probability (1-99 int) by its id."""
        return self._patch(f"/predictions/{prediction_id}",
                            {"probability": int(probability)})

    def results(self, lobby_id: str) -> list[dict]:
        """Settled predictions (closed + resolved markets) with brier scores."""
        return self._get("/results", lobby_id=lobby_id)
