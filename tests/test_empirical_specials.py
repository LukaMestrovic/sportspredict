import unittest

from bot import empirical_specials


def _history(*, historical, historical_ko, wc, wc_ko, overlap):
    def row(pair):
        yes, observations = pair
        return {
            "available": True, "yes_events": yes, "observations": observations,
            "rate": yes / observations,
        }
    return {
        "cohort_overlap": {"wc2026_in_all_history": row(overlap)},
        "empirical_rate": {
            "all_history": row(historical),
            "all_history_knockout": row(historical_ko),
            "wc2026": row(wc),
            "wc2026_knockout": row(wc_ko),
        },
    }


class EmpiricalSpecialTests(unittest.TestCase):
    def test_stoppage_snapshot_uses_each_match_once(self):
        result = empirical_specials.calibrate(_history(
            historical=(593, 4173), historical_ko=(56, 343),
            wc=(10, 101), wc_ko=(8, 29), overlap=(1, 34),
        ), 0.1317)

        self.assertEqual(result["model_version"], "nested-era-stage-logit-v1")
        self.assertAlmostEqual(result["probability"], 0.163887, places=6)
        self.assertEqual(
            [(row["yes_events"], row["observations"]) for row in result["disjoint_cells"]],
            [(536, 3796), (56, 343), (2, 72), (8, 29)],
        )
        self.assertEqual(sum(row["observations"] for row in result["disjoint_cells"]), 4240)

    def test_card_before_goal_snapshot(self):
        result = empirical_specials.calibrate(_history(
            historical=(1914, 4173), historical_ko=(180, 343),
            wc=(36, 101), wc_ko=(11, 29), overlap=(11, 34),
        ), 0.6016)

        self.assertAlmostEqual(result["probability"], 0.399644, places=6)
        self.assertEqual(result["disjoint_cells"][2]["yes_events"], 25)
        self.assertEqual(result["raw_simulator_probability"], 0.6016)

    def test_invalid_nested_counts_fail_closed(self):
        self.assertIsNone(empirical_specials.calibrate(_history(
            historical=(10, 100), historical_ko=(11, 20),
            wc=(5, 30), wc_ko=(2, 10), overlap=(1, 1),
        )))


if __name__ == "__main__":
    unittest.main()
