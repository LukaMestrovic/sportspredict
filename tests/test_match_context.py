import unittest

from bot import match_context


# Team ids: 100 = Alpha (target home), 200 = Beta (target away), 300/400 = others.
def _fixture(fid, home_id, home, away_id, away, gh, ga, date, ref=None):
    return {
        "fixture": {"id": fid, "date": date, "status": {"short": "FT"}, "referee": ref},
        "league": {"name": "World Cup"},
        "teams": {"home": {"id": home_id, "name": home},
                  "away": {"id": away_id, "name": away}},
        "goals": {"home": gh, "away": ga},
    }


_FIXTURES = [
    _fixture(1, 100, "Alpha", 300, "Gamma", 2, 1, "2026-06-20T00:00:00Z", "J. Smith"),
    _fixture(2, 400, "Delta", 100, "Alpha", 0, 0, "2026-06-15T00:00:00Z", "A. Other"),
    _fixture(3, 200, "Beta", 300, "Gamma", 1, 1, "2026-06-18T00:00:00Z", "J. Smith"),
]


def _stat(team_id, shots, sot, corners, fouls, offsides, yel, red, xg):
    return {
        "team": {"id": team_id},
        "statistics": [
            {"type": "Total Shots", "value": shots},
            {"type": "Shots on Goal", "value": sot},
            {"type": "Corner Kicks", "value": corners},
            {"type": "Fouls", "value": fouls},
            {"type": "Offsides", "value": offsides},
            {"type": "Yellow Cards", "value": yel},
            {"type": "Red Cards", "value": red},
            {"type": "expected_goals", "value": xg},
        ],
    }


_STATS = {
    1: [_stat(100, 14, 6, 7, 10, 2, 2, 0, "1.8"), _stat(300, 8, 3, 4, 12, 1, 3, 0, "0.9")],
    2: [_stat(400, 10, 4, 5, 9, 0, 1, 0, "1.1"), _stat(100, 9, 2, 3, 11, 1, 4, 1, "0.7")],
    3: [_stat(200, 12, 5, 6, 8, 2, 1, 0, "1.3"), _stat(300, 7, 2, 4, 13, 1, 2, 0, "0.6")],
}


def _player(name, minutes, sub, shots, on, goals):
    return {
        "player": {"name": name},
        "statistics": [{
            "games": {"minutes": minutes, "substitute": sub},
            "shots": {"total": shots, "on": on},
            "goals": {"total": goals},
        }],
    }


_FIXTURE_PLAYERS = {
    1: [{"team": {"id": 100}, "players": [
            _player("Striker One", 90, False, 4, 2, 1),
            _player("Mid Two", 90, False, 1, 0, 0),
            _player("Sub Three", 20, True, 1, 1, 0)]},
        {"team": {"id": 300}, "players": [_player("Gamma Guy", 90, False, 2, 1, 0)]}],
    2: [{"team": {"id": 100}, "players": [
            _player("Striker One", 80, False, 3, 1, 0),
            _player("Mid Two", 90, False, 0, 0, 0)]},
        {"team": {"id": 400}, "players": [_player("Delta Dude", 90, False, 1, 0, 0)]}],
    3: [{"team": {"id": 200}, "players": [_player("Beta Boss", 90, False, 5, 3, 1)]}],
}


# A non-WC fixture (different competition) the same referee also officiated,
# proving the profile spans more than this tournament.
_OTHER_LEAGUE_FX = {
    "fixture": {"id": 50, "date": "2026-05-01T00:00:00Z", "status": {"short": "FT"},
                "referee": "J. Smith"},
    "league": {"name": "Primeira Liga"},
    "teams": {"home": {"id": 900, "name": "Porto"}, "away": {"id": 901, "name": "Benfica"}},
    "goals": {"home": 1, "away": 1},
}
_STATS[50] = [_stat(900, 10, 4, 5, 12, 1, 4, 0, "1.0"),
              _stat(901, 9, 3, 4, 13, 2, 2, 1, "0.9")]


class _FakeAF:
    def __init__(self, *, fail=None):
        self.fail = fail or set()

    def fixtures(self):
        if "fixtures" in self.fail:
            raise RuntimeError("boom")
        return _FIXTURES

    def league_fixtures(self, league_id, season):
        if "league" in self.fail:
            raise RuntimeError("boom")
        # One configured league/season returns a Pinheiro match in another comp.
        if league_id == 94 and season == 2025:
            return [_OTHER_LEAGUE_FX]
        return []

    def settled_statistics(self, fid):
        if "stats" in self.fail:
            raise RuntimeError("boom")
        return _STATS.get(fid, [])

    def fixture_players(self, fid):
        if "players" in self.fail:
            raise RuntimeError("boom")
        return _FIXTURE_PLAYERS.get(fid, [])

    def injuries(self, team_id, season):
        if "injuries" in self.fail:
            raise RuntimeError("boom")
        if team_id == 100:
            return [{"player": {"name": "Striker One", "type": "Questionable",
                                "reason": "Knock"}},
                    {"player": {"name": "Striker One", "type": "dup", "reason": "x"}}]
        return []


_TARGET = _fixture(999, 100, "Alpha", 200, "Beta", 0, 0, "2026-06-28T00:00:00Z", "J. Smith")
_LINEUPS = [
    {"team": {"id": 100}, "startXI": [{"player": {"name": "Striker One"}}],
     "substitutes": [{"player": {"name": "Mid Two"}}]},
]


class MatchContextTests(unittest.TestCase):
    def test_team_form_aggregates_results_and_stats(self):
        ctx = match_context.build(_FakeAF(), _TARGET, "Alpha", "Beta", None)
        home = ctx["team_form"]["home"]
        self.assertEqual(home["games"], 2)
        self.assertEqual(home["gf_avg"], 1.0)        # (2 + 0) / 2
        self.assertEqual(home["ga_avg"], 0.5)        # (1 + 0) / 2
        self.assertEqual(home["clean_sheet_rate"], 0.5)
        self.assertEqual(home["btts_rate"], 0.5)
        self.assertEqual(home["over25_rate"], 0.5)   # f1 total 3, f2 total 0
        self.assertAlmostEqual(home["avg_shots"], 11.5)   # (14 + 9) / 2
        self.assertAlmostEqual(home["avg_cards"], 3.5)    # (2) and (4+1)
        self.assertAlmostEqual(home["xg_for_avg"], 1.25)  # (1.8 + 0.7) / 2
        self.assertAlmostEqual(home["xg_against_avg"], 1.0)  # (0.9 + 1.1) / 2

    def test_player_form_per90_and_lineup_scope(self):
        ctx = match_context.build(_FakeAF(), _TARGET, "Alpha", "Beta", _LINEUPS)
        home = ctx["player_form"]["home"]
        names = [p["name"] for p in home]
        # Lineup scoping keeps only Striker One + Mid Two (drops Sub Three).
        self.assertEqual(set(names), {"Striker One", "Mid Two"})
        striker = next(p for p in home if p["name"] == "Striker One")
        self.assertEqual(striker["minutes"], 170)    # 90 + 80
        self.assertEqual(striker["starts"], 2)
        self.assertEqual(striker["shots"], 7)        # 4 + 3
        self.assertEqual(striker["goals"], 1)
        self.assertAlmostEqual(striker["sot_per90"], round(3 / 170 * 90, 2))

    def test_player_index_keeps_players_dropped_from_capped_list(self):
        ctx = match_context.build(_FakeAF(), _TARGET, "Alpha", "Beta", _LINEUPS)
        # Sub Three is scoped out of the list but must remain in the full index,
        # so a named prop on a bench player can still find an exact row.
        self.assertNotIn("Sub Three", [p["name"] for p in ctx["player_form"]["home"]])
        self.assertIn("Sub Three", ctx["player_index"])
        self.assertEqual(ctx["player_index"]["Sub Three"]["minutes"], 20)

    def test_referee_profile_spans_competitions(self):
        ctx = match_context.build(_FakeAF(), _TARGET, "Alpha", "Beta", None)
        ref = ctx["referee_profile"]
        self.assertEqual(ref["name"], "J. Smith")
        # WC fixtures 1 and 3 plus the Primeira Liga fixture 50.
        self.assertEqual(ref["games"], 3)
        self.assertEqual(ref["competitions"], {"World Cup": 2, "Primeira Liga": 1})
        self.assertAlmostEqual(ref["yellows_per_game"], round((5 + 3 + 6) / 3, 2))
        self.assertAlmostEqual(ref["reds_per_game"], round(1 / 3, 2))

    def test_referee_profile_empty_on_name_miss(self):
        target = _fixture(999, 100, "Alpha", 200, "Beta", 0, 0,
                          "2026-06-28T00:00:00Z", "Nobody At All")
        ctx = match_context.build(_FakeAF(), target, "Alpha", "Beta", None)
        self.assertEqual(ctx["referee_profile"], {})

    def test_injuries_dedup_by_player(self):
        ctx = match_context.build(_FakeAF(), _TARGET, "Alpha", "Beta", None)
        self.assertEqual(ctx["injuries"]["home"],
                         [{"player": "Striker One", "type": "Questionable", "reason": "Knock"}])
        self.assertEqual(ctx["injuries"]["away"], [])

    def test_each_block_degrades_independently(self):
        ctx = match_context.build(
            _FakeAF(fail={"players", "injuries"}),
            _TARGET, "Alpha", "Beta", None,
        )
        # Team form and referee still work; the failing blocks fall back to empty.
        # Player form degrades per-fixture, so it stays a well-formed empty dict.
        self.assertTrue(ctx["team_form"]["home"])
        self.assertTrue(ctx["referee_profile"])
        self.assertEqual(ctx["player_form"], {"home": [], "away": []})
        self.assertEqual(ctx["injuries"], {})

    def test_total_fetch_failure_yields_empty_blocks(self):
        ctx = match_context.build(_FakeAF(fail={"fixtures"}), _TARGET, "Alpha", "Beta", None)
        self.assertEqual(ctx["team_form"], {"home": {}, "away": {}})
        self.assertEqual(ctx["player_form"], {"home": [], "away": []})


if __name__ == "__main__":
    unittest.main()
