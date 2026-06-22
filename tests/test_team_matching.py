import unittest

from bot.apifootball import APIFootball
from bot.oddsapi import OddsAPI
from bot.teams import normalize_team, split_match_name


class TeamNormalizationTests(unittest.TestCase):
    def test_tournament_codes_do_not_confuse_australia_and_austria(self):
        self.assertEqual(split_match_name("PAR vs AUS"), ("paraguay", "australia"))
        self.assertEqual(split_match_name("ARG vs AUT"), ("argentina", "austria"))

    def test_provider_variants_normalize(self):
        self.assertEqual(normalize_team("Bosnia & Herzegovina"), "bosnia herzegovina")
        self.assertEqual(normalize_team("DR Congo"), normalize_team("Congo DR"))
        self.assertEqual(normalize_team("Curaçao"), normalize_team("Curacao"))
        self.assertEqual(normalize_team("Cape Verde Islands"), normalize_team("CPV"))


class ProviderMatchingTests(unittest.TestCase):
    def test_api_football_disambiguates_shared_kickoff(self):
        af = APIFootball("unused")
        af._fixtures_cache = [
            _fixture(1, "2026-06-24T19:00:00Z", "Switzerland", "Canada"),
            _fixture(2, "2026-06-24T19:00:00Z", "Bosnia & Herzegovina", "Qatar"),
        ]
        self.assertEqual(af.find_fixture("2026-06-24T19:00:00Z", "BIH vs QAT")["fixture"]["id"], 2)

    def test_odds_api_disambiguates_provider_name_order(self):
        oa = OddsAPI("unused")
        oa._events = [
            _event("a", "Colombia", "Portugal"),
            _event("b", "DR Congo", "Uzbekistan"),
        ]
        found = oa.find_event("2026-06-27T23:30:00Z", "Congo DR", "Uzbekistan")
        self.assertEqual(found["id"], "b")


def _fixture(fixture_id, kickoff, home, away):
    return {
        "fixture": {"id": fixture_id, "date": kickoff},
        "teams": {"home": {"name": home}, "away": {"name": away}},
    }


def _event(event_id, home, away):
    return {
        "id": event_id, "commence_time": "2026-06-27T23:30:00Z",
        "home_team": home, "away_team": away,
    }


if __name__ == "__main__":
    unittest.main()
