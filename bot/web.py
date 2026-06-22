"""Client for the SportPredict *web* API (base `…/api`, not `…/api/v1`).

The bot/REST API (`/api/v1`) intentionally hides crowd consensus. The public
web API used by the site exposes, for SETTLED markets, both the crowd mean and
the realized outcome — exactly what we need for a bot-vs-crowd post-mortem. The
same bot bearer key authenticates here.

Key routes:
  GET  /matches/event/more-matches?eventId&tab=settled&limit&skip
       -> {items:[match...], total, counts}
  POST /probability/match-crowd-stats {matchId, lobbyId}
       -> {markets:[{id, question, current_value(0|100), prediction_average(0-100), status}]}
"""
from __future__ import annotations

import requests

from . import config

WEB_BASE = "https://api.sportspredict.com/api"


class WebAPI:
    def __init__(self, key: str | None = None):
        self.key = key or config.SPORTSPREDICT_KEY
        self.s = requests.Session()
        self.s.headers["Authorization"] = f"Bearer {self.key}"

    def settled_matches(self, event_id: str, limit: int = 40) -> list[dict]:
        """Settled matches, most-recent first. Paginates via `skip` (page=8)."""
        out: list[dict] = []
        skip = 0
        while len(out) < limit:
            r = self.s.get(
                f"{WEB_BASE}/matches/event/more-matches",
                params={"eventId": event_id, "tab": "settled", "limit": 8, "skip": skip},
                timeout=30,
            )
            r.raise_for_status()
            items = r.json().get("items", [])
            if not items:
                break
            out.extend(items)
            skip += 8
        return out[:limit]

    def crowd_stats(self, match_id: str, lobby_id: str) -> list[dict]:
        """Per-market crowd mean + outcome for one settled match."""
        r = self.s.post(
            f"{WEB_BASE}/probability/match-crowd-stats",
            json={"matchId": match_id, "lobbyId": lobby_id},
            timeout=30,
        )
        r.raise_for_status()
        return r.json().get("markets", [])
