import unittest
from unittest.mock import patch

from bot import lineups


class LineupFallbackTests(unittest.TestCase):
    def test_api_football_lineups_win_when_present(self):
        af = _AF([{"team": {"name": "API Team"}}])
        fixture = _fixture()

        with patch("bot.lineups.FIFA") as fifa:
            got = lineups.fetch_lineups(af, fixture, refresh=True)

        self.assertEqual(got, [{"team": {"name": "API Team"}}])
        fifa.assert_not_called()

    def test_fifa_used_when_api_football_empty(self):
        af = _AF([])
        fixture = _fixture()
        expected = [{"team": {"name": "FIFA Team"}, "source": "fifa"}]

        with patch("bot.lineups.FIFA") as fifa:
            fifa.return_value.lineups_for_match.return_value = expected
            got = lineups.fetch_lineups(af, fixture, refresh=True)

        self.assertEqual(got, expected)
        fifa.assert_called_once_with(refresh=True)
        fifa.return_value.lineups_for_match.assert_called_once_with(
            "2026-07-01T16:00:00Z", "England", "Congo DR",
        )


class _AF:
    def __init__(self, response):
        self.response = response

    def lineups(self, fixture_id):
        self.fixture_id = fixture_id
        return self.response


def _fixture():
    return {
        "fixture": {"id": 42, "date": "2026-07-01T16:00:00Z"},
        "teams": {
            "home": {"name": "England"},
            "away": {"name": "Congo DR"},
        },
    }


if __name__ == "__main__":
    unittest.main()
