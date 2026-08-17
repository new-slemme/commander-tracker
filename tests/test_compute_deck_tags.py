import os
import tempfile
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


class ComputeCommanderBracketTests(unittest.TestCase):
    def test_high_signal_list_maps_to_bracket_5(self):
        result = app.compute_commander_bracket([
            "Mana Crypt",
            "Jeweled Lotus",
            "Demonic Tutor",
            "Thassa's Oracle",
            "Demonic Consultation",
        ])

        self.assertEqual(result["bracket"], 5)
        self.assertGreaterEqual(result["score"], 7)

    def test_empty_list_maps_to_bracket_1(self):
        result = app.compute_commander_bracket([])
        self.assertEqual(result["bracket"], 1)

    def test_api_supports_cards_and_text(self):
        client = app.app.test_client()

        by_cards = client.post(
            "/api/commander-bracket",
            json={"cards": ["Mana Crypt", "Demonic Tutor"]},
        )
        self.assertEqual(by_cards.status_code, 200)
        self.assertIn("bracket", by_cards.get_json())

        by_text = client.post(
            "/api/commander-bracket",
            json={"decklist_text": "1 Mana Crypt\n1 Island"},
        )
        self.assertEqual(by_text.status_code, 200)
        self.assertIn("signals", by_text.get_json())

        invalid = client.post("/api/commander-bracket", json={"foo": "bar"})
        self.assertEqual(invalid.status_code, 400)


class DeckDetailCommanderBracketTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmpdir = tempfile.TemporaryDirectory()
        cls._db_path = os.path.join(cls._tmpdir.name, "deck_detail_bracket.sqlite")
        app.app.config.update(
            TESTING=True,
            SQLALCHEMY_DATABASE_URI=f"sqlite:///{cls._db_path}",
            WTF_CSRF_ENABLED=False,
        )

    @classmethod
    def tearDownClass(cls):
        cls._tmpdir.cleanup()

    def setUp(self):
        with app.app.app_context():
            app.db.session.remove()
            app.db.drop_all()
            app.db.create_all()

    def test_deck_detail_displays_commander_bracket(self):
        with app.app.app_context():
            user = app.User(
                username="bracket-tester",
                display_name="Bracket Tester",
                password_hash="unused",
                is_active=True,
            )
            app.db.session.add(user)
            app.db.session.flush()
            player = app.Player(name="Bracket Tester", user_id=user.id)
            app.db.session.add(player)
            app.db.session.flush()
            pod = app.Pod(name="Bracket Pod", slug="bracket-pod")
            app.db.session.add(pod)
            app.db.session.flush()
            app.db.session.add(app.PodMembership(pod_id=pod.id, player_id=player.id))

            deck = app.Deck(
                name="Bracket Deck",
                commander="Kess, Dissident Mage",
                player_id=player.id,
                decklist_text="""1 Commander
1 Kess, Dissident Mage
1 Mana Crypt
1 Demonic Tutor
1 Thassa's Oracle
1 Demonic Consultation
""",
            )
            app.db.session.add(deck)
            app.db.session.commit()

            client = app.app.test_client()
            with client.session_transaction() as session:
                session["user_id"] = user.id
            response = client.get(f"/deck/{deck.id}")

        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn("Commander Bracket 5", html)


if __name__ == "__main__":
    unittest.main()
