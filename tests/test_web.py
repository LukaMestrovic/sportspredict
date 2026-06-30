import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bot import cache
from bot.web import WebAPI


class _Response:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class WebAPITests(unittest.TestCase):
    def test_settled_matches_paginates_and_caches(self):
        web = WebAPI("unused")
        calls = []

        def get(_url, *, params, timeout):
            calls.append(params["skip"])
            items = [{"id": "m1"}, {"id": "m2"}] if params["skip"] == 0 else []
            return _Response({"items": items})

        web.s.get = get
        with tempfile.TemporaryDirectory() as directory, patch.object(
            cache, "CACHE_DIR", Path(directory)
        ):
            first = web.settled_matches("event")
            second = web.settled_matches("event")

        self.assertEqual(first, [{"id": "m1"}, {"id": "m2"}])
        self.assertEqual(second, first)
        self.assertEqual(calls, [0, 2])

    def test_settled_market_outcomes_cache_forever(self):
        web = WebAPI("unused")
        calls = []
        web.crowd_stats = lambda match_id, lobby_id: (
            calls.append((match_id, lobby_id)) or [{"id": "q1", "current_value": 100}]
        )
        with tempfile.TemporaryDirectory() as directory, patch.object(
            cache, "CACHE_DIR", Path(directory)
        ):
            first = web.settled_crowd_stats("match", "lobby")
            second = web.settled_crowd_stats("match", "lobby")

        self.assertEqual(second, first)
        self.assertEqual(calls, [("match", "lobby")])


if __name__ == "__main__":
    unittest.main()
