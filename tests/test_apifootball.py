import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bot import cache
from bot.apifootball import APIFootball


class NewEndpointTests(unittest.TestCase):
    def _client(self, response, *, refresh_odds=False):
        af = APIFootball("unused", refresh_odds=refresh_odds)
        self.calls = []

        def _get(path, **params):
            self.calls.append((path, params))
            return {"response": response}

        af._get = _get
        return af

    def test_fixture_players_hits_endpoint_and_caches_forever(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(
            cache, "CACHE_DIR", Path(tmp)
        ):
            af = self._client([{"team": {"id": 1}, "players": []}])
            out = af.fixture_players(42)
            # Second call returns the cached value without another network hit.
            again = af.fixture_players(42)

        self.assertEqual(out, [{"team": {"id": 1}, "players": []}])
        self.assertEqual(again, out)
        self.assertEqual(self.calls, [("/fixtures/players", {"fixture": 42})])

    def test_injuries_refreshes_with_refresh_odds(self):
        responses = iter([
            [{"player": {"name": "Old"}}],
            [{"player": {"name": "New"}}],
        ])
        with tempfile.TemporaryDirectory() as tmp, patch.object(
            cache, "CACHE_DIR", Path(tmp)
        ):
            stale = APIFootball("unused")
            stale._get = lambda *_a, **_k: {"response": next(responses)}
            self.assertEqual(stale.injuries(5, 2026)[0]["player"]["name"], "Old")

            fresh = APIFootball("unused", refresh_odds=True)
            fresh._get = lambda *_a, **_k: {"response": next(responses)}
            self.assertEqual(fresh.injuries(5, 2026)[0]["player"]["name"], "New")


if __name__ == "__main__":
    unittest.main()
