import json
import unittest
from pathlib import Path

from bot import matcher
from scripts import export_market_catalog


class MarketCatalogDocsTests(unittest.TestCase):
    def test_generated_market_catalog_docs_are_current(self):
        catalog = export_market_catalog.build_catalog()
        self.assertEqual(
            json.loads(Path("docs/market_catalog.json").read_text()),
            catalog,
        )
        self.assertEqual(
            Path("docs/market_catalog.md").read_text(),
            export_market_catalog.render_markdown(catalog),
        )

    def test_provider_catalog_and_open_question_audit_are_retained(self):
        self.assertTrue(Path("soccer_live_odds_market_catalog.pdf").is_file())
        self.assertTrue(Path("analysis/wc2026_open_questions_20260630.md").is_file())

    def test_every_parser_market_key_is_documented(self):
        catalog = export_market_catalog.build_catalog()
        documented = {
            entry["intent_market"]
            for section in ("api_football", "odds_api", "provider_gaps")
            for entry in catalog[section]
        }
        self.assertLessEqual(set(matcher.MARKET_KEYS), documented)


if __name__ == "__main__":
    unittest.main()
