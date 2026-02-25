import unittest
from unittest.mock import patch

import app


class ComputeDeckTagsTests(unittest.TestCase):
    def test_lookup_failure_does_not_abort_other_tag_detection(self):
        lookup_payloads = {
            "Good Monarch Card": {"oracle_text": "When this enters, you become the monarch."},
            "Energy Card": {"oracle_text": "You get {E}{E}."},
        }

        def fake_lookup(name: str):
            if name == "Missing Card":
                return None
            return lookup_payloads.get(name)

        with patch("app.scryfall_named_exact", side_effect=fake_lookup):
            tags, diagnostics = app.compute_deck_tags(
                ["Good Monarch Card", "Missing Card", "Energy Card"]
            )

        self.assertTrue(tags["monarch"])
        self.assertTrue(tags["energy"])
        self.assertEqual(diagnostics["unresolved_count"], 1)
        self.assertEqual(diagnostics["unresolved_cards"], ["Missing Card"])


if __name__ == "__main__":
    unittest.main()
