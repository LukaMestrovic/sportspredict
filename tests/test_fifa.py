import unittest

from bot.fifa import FIFA, _parse_lineups


class FifaLineupTests(unittest.TestCase):
    def test_find_match_accepts_cabo_verde_official_name(self):
        client = FIFA()
        client.calendar = lambda: [{
            "IdMatch": "400021521",
            "Date": "2026-07-03T22:00:00Z",
            "Home": _calendar_team("Argentina", "ARG"),
            "Away": _calendar_team("Cabo Verde", "CPV"),
        }]

        found = client.find_match(
            "2026-07-03T22:00:00+00:00",
            "Argentina",
            "Cape Verde Islands",
        )

        self.assertEqual(found["IdMatch"], "400021521")

    def test_parse_live_match_lineups_to_api_football_shape(self):
        payload = {
            "IdMatch": "400",
            "HomeTeam": _team("Portugal", "POR"),
            "AwayTeam": _team("Uzbekistan", "UZB"),
        }

        lineups = _parse_lineups(payload)

        self.assertEqual(len(lineups), 2)
        home = lineups[0]
        self.assertEqual(home["team"]["name"], "Portugal")
        self.assertEqual(home["formation"], "4-2-3-1")
        self.assertEqual(home["source"], "fifa")
        self.assertEqual(home["provider_match_id"], "400")
        self.assertEqual(len(home["startXI"]), 11)
        self.assertEqual(len(home["substitutes"]), 2)
        self.assertEqual(home["startXI"][0]["player"]["pos"], "G")
        self.assertEqual(home["substitutes"][0]["player"]["name"], "Portugal Sub 1")

    def test_incomplete_starters_return_empty(self):
        payload = {
            "HomeTeam": _team("A", "AAA", starters=10),
            "AwayTeam": _team("B", "BBB"),
        }
        self.assertEqual(_parse_lineups(payload), [])


def _team(name, code, starters=11):
    players = []
    for i in range(starters):
        players.append(_player(f"{name} Starter {i + 1}", i + 1, 1, i % 4))
    for i in range(2):
        players.append(_player(f"{name} Sub {i + 1}", i + 20, 2, 3))
    return {
        "IdTeam": "123",
        "TeamName": [{"Locale": "en-GB", "Description": name}],
        "Abbreviation": code,
        "Tactics": "4-2-3-1",
        "Players": players,
        "Coaches": [{
            "IdCoach": "9",
            "Role": 0,
            "Alias": [{"Locale": "en-GB", "Description": f"{name} Coach"}],
        }],
    }


def _calendar_team(name, code):
    return {
        "TeamName": [{"Locale": "en-GB", "Description": name}],
        "Abbreviation": code,
    }


def _player(name, number, status, position):
    return {
        "IdPlayer": str(number),
        "ShirtNumber": number,
        "Status": status,
        "Position": position,
        "PlayerName": [{"Locale": "en-GB", "Description": name}],
    }


if __name__ == "__main__":
    unittest.main()
