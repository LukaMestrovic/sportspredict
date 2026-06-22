import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bot import cache
from bot.apifootball import APIFootball
from bot.oddsapi import OddsAPI


class CacheRefreshTests(unittest.TestCase):
    def test_refresh_replaces_a_live_cache_entry(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(
            cache, "CACHE_DIR", Path(tmp)
        ):
            first = cache.get_or_fetch("test", "key", lambda: "old", ttl=3600)
            fresh = cache.get_or_fetch(
                "test", "key", lambda: "new", ttl=3600, refresh=True
            )
            reused = cache.get_or_fetch("test", "key", lambda: "wrong", ttl=3600)

        self.assertEqual((first, fresh, reused), ("old", "new", "new"))


class ProviderRefreshTests(unittest.TestCase):
    def test_api_football_refreshes_disk_once_per_client(self):
        responses = iter([
            {"response": [{"bookmakers": [{"id": "old"}]}]},
            {"response": [{"bookmakers": [{"id": "new"}]}]},
        ])
        with tempfile.TemporaryDirectory() as tmp, patch.object(
            cache, "CACHE_DIR", Path(tmp)
        ):
            old = APIFootball("unused")
            old._get = lambda *_args, **_kwargs: next(responses)
            self.assertEqual(old.odds(1)[0]["id"], "old")

            fresh = APIFootball("unused", refresh_odds=True)
            fresh._get = lambda *_args, **_kwargs: next(responses)
            self.assertEqual(fresh.odds(1)[0]["id"], "new")
            self.assertEqual(fresh.odds(1)[0]["id"], "new")

    def test_odds_api_refreshes_disk_once_per_market_and_client(self):
        calls = []

        def response(*_args, **_kwargs):
            calls.append(1)
            return {"bookmakers": [{"key": f"book-{len(calls)}"}]}

        with tempfile.TemporaryDirectory() as tmp, patch.object(
            cache, "CACHE_DIR", Path(tmp)
        ):
            old = OddsAPI("unused")
            old._get = response
            self.assertEqual(old.event_odds("event", ["h2h"])[0]["key"], "book-1")

            fresh = OddsAPI("unused", refresh_odds=True)
            fresh._get = response
            self.assertEqual(
                fresh.event_odds("event", ["h2h"])[0]["key"], "book-2"
            )
            self.assertEqual(
                fresh.event_odds("event", ["h2h"])[0]["key"], "book-2"
            )

        self.assertEqual(len(calls), 2)


if __name__ == "__main__":
    unittest.main()
