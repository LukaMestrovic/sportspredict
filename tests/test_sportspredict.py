import unittest

from bot.sportspredict import EVENT_TITLE, SportPredict


class SportPredictDiscoveryTests(unittest.TestCase):
    def test_target_event_is_selected(self):
        client = SportPredict("unused")
        client._get = lambda *_args, **_kwargs: [
            {"id": "other", "title": "Other"},
            {"id": "cup", "title": EVENT_TITLE},
        ]
        self.assertEqual(client.event()["id"], "cup")

    def test_missing_target_event_fails_closed(self):
        client = SportPredict("unused")
        client._get = lambda *_args, **_kwargs: [
            {"id": "other", "title": "Other"},
        ]
        with self.assertRaisesRegex(LookupError, "required SportPredict event"):
            client.event()

    def test_empty_lobby_list_fails_closed(self):
        client = SportPredict("unused")
        client._get = lambda *_args, **_kwargs: []
        with self.assertRaisesRegex(LookupError, "no SportPredict lobby"):
            client.lobby("cup")


if __name__ == "__main__":
    unittest.main()
