import unittest
from unittest.mock import MagicMock, patch

import deck_import


class NullOracleCardTests(unittest.TestCase):
    """TASK-R08: a JSON `"oracleCard": null` must not raise AttributeError."""

    def test_merge_mapping_cards_falls_back_when_oracle_card_is_null(self):
        buckets: dict = {}
        payload = [
            {
                "quantity": 1,
                "card": {"name": None, "oracleCard": None},
                "name": "Fallback Name",
            }
        ]

        deck_import._merge_mapping_cards(buckets, "mainboard", payload)

        self.assertIn("mainboard", buckets)
        self.assertIn("fallback name", buckets["mainboard"])
        self.assertEqual(buckets["mainboard"]["fallback name"].name, "Fallback Name")
        self.assertEqual(buckets["mainboard"]["fallback name"].quantity, 1)

    def test_parse_archidekt_url_falls_back_when_oracle_card_is_null(self):
        fake_response = MagicMock()
        fake_response.status_code = 200
        fake_response.json.return_value = {
            "cards": [
                {
                    "quantity": 1,
                    "categories": [],
                    "card": {"oracleCard": None, "name": "Fallback Name"},
                }
            ]
        }

        with patch("deck_import.requests.get", return_value=fake_response):
            parsed = deck_import.parse_archidekt_url("https://archidekt.com/decks/123456")

        names = [entry.name for entry in parsed.sections["mainboard"]]
        self.assertIn("Fallback Name", names)


if __name__ == "__main__":
    unittest.main()
