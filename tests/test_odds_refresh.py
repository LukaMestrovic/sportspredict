import tempfile
import unittest
import json
from pathlib import Path
from unittest.mock import Mock, patch

import requests

from bot import cache
from bot.apifootball import APIFootball
from bot.oddsapi import OddsAPI, OddsAPIRequestError


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

    def test_corrupt_cache_entry_is_replaced_atomically(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(
            cache, "CACHE_DIR", Path(tmp)
        ):
            path = cache._path("test", "key")
            path.parent.mkdir(parents=True)
            path.write_text('{"partial":')
            value = cache.get_or_fetch("test", "key", lambda: "recovered")
            stored = json.loads(path.read_text())

        self.assertEqual(value, "recovered")
        self.assertEqual(stored["value"], "recovered")


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
        self.assertEqual(len(fresh.observations), 1)
        self.assertEqual(fresh.observations[0]["markets"], ["h2h"])

    def test_odds_api_fresh_refreshes_event_listing_once(self):
        calls = []
        with tempfile.TemporaryDirectory() as tmp, patch.object(
            cache, "CACHE_DIR", Path(tmp)
        ):
            stale = OddsAPI("unused")
            stale._get = lambda *_args, **_kwargs: (
                calls.append("stale") or [{"id": "old"}]
            )
            self.assertEqual(stale.events()[0]["id"], "old")

            fresh = OddsAPI("unused", refresh_odds=True)
            fresh._get = lambda *_args, **_kwargs: (
                calls.append("fresh") or [{"id": "new"}]
            )
            self.assertEqual(fresh.events()[0]["id"], "new")
            self.assertEqual(fresh.events()[0]["id"], "new")

        self.assertEqual(calls, ["stale", "fresh"])


class OddsAPIErrorTests(unittest.TestCase):
    def test_auth_error_is_sanitized_and_not_downgraded(self):
        response = Mock(status_code=401)
        response.raise_for_status.side_effect = requests.HTTPError(
            "401 for https://example.test?apiKey=super-secret", response=response,
        )
        with patch("bot.oddsapi.requests.get", return_value=response):
            with self.assertRaises(OddsAPIRequestError) as raised:
                OddsAPI("super-secret")._get("/sports/test/events")
        self.assertNotIn("super-secret", str(raised.exception))
        self.assertEqual(raised.exception.status_code, 401)

    def test_only_422_market_unavailability_becomes_empty_odds(self):
        for status, should_raise in ((422, False), (401, True), (429, True), (500, True)):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as tmp, \
                 patch.object(cache, "CACHE_DIR", Path(tmp)):
                client = OddsAPI("unused")
                client._get = Mock(side_effect=OddsAPIRequestError("/odds", status))
                if should_raise:
                    with self.assertRaises(OddsAPIRequestError):
                        client.event_odds("event", ["h2h"])
                else:
                    self.assertEqual(client.event_odds("event", ["h2h"]), [])


if __name__ == "__main__":
    unittest.main()
