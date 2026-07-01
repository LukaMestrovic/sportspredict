import os
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SIMULATOR = ROOT / "simulator"
if str(SIMULATOR / "src") not in sys.path:
    sys.path.insert(0, str(SIMULATOR / "src"))
os.environ.setdefault("SPORTSPREDICT_ROOT", str(SIMULATOR))


class SimulatorTrainingPipelineTests(unittest.TestCase):
    def test_team_ratings_json_round_trip(self):
        import pandas as pd

        from sphybrid.rates.team_ratings import TeamRatings, fit_team_ratings

        results = pd.DataFrame([
            {"home_team": "A", "away_team": "B", "home_score": 2, "away_score": 0, "neutral": True},
            {"home_team": "A", "away_team": "C", "home_score": 1, "away_score": 1, "neutral": True},
            {"home_team": "B", "away_team": "C", "home_score": 0, "away_score": 1, "neutral": True},
            {"home_team": "C", "away_team": "A", "home_score": 0, "away_score": 2, "neutral": True},
        ])
        ratings = fit_team_ratings(results, min_matches=1)

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "team_ratings.json"
            ratings.save(path)
            loaded = TeamRatings.load(path)

        self.assertIn("A", loaded.attack)
        self.assertEqual(set(loaded.attack), set(ratings.attack))
        self.assertEqual(set(loaded.defense), set(ratings.defense))
        self.assertEqual(set(loaded.n_matches), set(ratings.n_matches))

    def test_player_shares_write_runtime_json(self):
        import pandas as pd

        from sphybrid.postsim.allocation import PlayerShares
        from sphybrid.postsim.fit_shares import write_shares

        shares = pd.DataFrame([
            {
                "player": "Example Forward",
                "team": "A",
                "stat": "shots_on_target",
                "share": 0.22,
                "per90": 1.4,
                "n_app": 3,
                "position": "FW",
            }
        ])

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "player_shares.json"
            write_shares(shares, path)
            loaded = PlayerShares.load(path)

        self.assertIsNotNone(loaded)
        self.assertAlmostEqual(loaded.get("Example Forward", "shots_on_target"), 0.22)
        self.assertEqual(loaded.team("Example Forward"), "A")

    def test_knockout_regulation_labels_use_only_safe_full_stat_sources(self):
        import pandas as pd

        from sphybrid.postsim.backtest_exotics import build_question_table

        history = pd.DataFrame([
            {
                "match_id": 1,
                "source": "statsbomb",
                "match_date": "2022-12-09",
                "home_team": "Home",
                "away_team": "Away",
                "tournament": "WC2022",
                "stage": "knockout",
                "home_goals_h1": 1,
                "home_goals_h2": 0,
                "away_goals_h1": 1,
                "away_goals_h2": 1,
                "home_shots_on_target_h1": 3,
                "home_shots_on_target_h2": 3,
                "away_shots_on_target_h1": 1,
                "away_shots_on_target_h2": 1,
            },
            {
                "match_id": 2,
                "source": "apifootball",
                "match_date": "2022-12-10",
                "home_team": "Home",
                "away_team": "Away",
                "tournament": "WC2022",
                "stage": "knockout",
                "home_goals_h1": 1,
                "home_goals_h2": 0,
                "away_goals_h1": 1,
                "away_goals_h2": 1,
                "home_shots_on_target_h1": 6,
                "home_shots_on_target_h2": 0,
                "away_shots_on_target_h1": 2,
                "away_shots_on_target_h2": 0,
            },
        ])
        events = pd.DataFrame(columns=[
            "source", "match_id", "event_type", "team_side", "phase",
            "minute", "extra", "sequence",
        ])
        players = pd.DataFrame(columns=[
            "match_id", "team_side", "reconciles_sot", "shots_total",
            "shots_on", "goals", "substitute",
        ])

        questions = build_question_table(history, events, players)
        full_stat = questions[
            questions.contract_key.eq("count:shots_on_target:team:full:>=:6:reg")
        ]
        btts = questions[questions.contract_key.eq("btts_and_total:reg")]

        self.assertEqual(set(full_stat.source.astype(str)), {"statsbomb"})
        self.assertEqual(set(btts.source.astype(str)), {"statsbomb", "apifootball"})


if __name__ == "__main__":
    unittest.main()
