import json
import os
import tempfile
import unittest
from unittest.mock import patch

import app


class ApiDecksWriteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmpdir = tempfile.TemporaryDirectory()
        cls._db_path = os.path.join(cls._tmpdir.name, "api_decks_write.sqlite")
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

    def _seed_users_players(self):
        owner_user = app.User(
            username="owner-user",
            display_name="Owner User",
            password_hash="hashed",
            is_active=True,
            is_admin=False,
        )
        other_user = app.User(
            username="other-user",
            display_name="Other User",
            password_hash="hashed",
            is_active=True,
            is_admin=False,
        )
        app.db.session.add_all([owner_user, other_user])
        app.db.session.flush()

        owner_player = app.Player(name="Owner Player", user_id=owner_user.id)
        other_player = app.Player(name="Other Player", user_id=other_user.id)
        app.db.session.add_all([owner_player, other_player])
        app.db.session.commit()
        return owner_user, owner_player, other_user, other_player

    def _auth_client_for(self, user_id):
        client = app.app.test_client()
        with client.session_transaction() as session:
            session["user_id"] = user_id
        return client

    @staticmethod
    def _fake_commander_metadata(commander_name: str):
        return {
            "commander": commander_name,
            "commander_name": commander_name,
            "commander_scryfall_id": None,
            "commander_art_crop_url": None,
            "commander_local_art_crop": None,
            "color_identity": "U",
        }

    def test_non_admin_can_create_deck_for_own_player(self):
        with app.app.app_context():
            owner_user, owner_player, _, _ = self._seed_users_players()
            client = self._auth_client_for(owner_user.id)

            with patch("app.resolve_commander_metadata", side_effect=self._fake_commander_metadata):
                response = client.post(
                    "/api/decks",
                    json={
                        "name": "Owner Deck",
                        "commander": "Talrand, Sky Summoner",
                    },
                )

            self.assertEqual(response.status_code, 201)
            payload = response.get_json()
            self.assertEqual(payload["name"], "Owner Deck")
            self.assertEqual(payload["player_id"], owner_player.id)

    def test_non_admin_cannot_create_deck_for_another_player(self):
        with app.app.app_context():
            owner_user, _, _, other_player = self._seed_users_players()
            client = self._auth_client_for(owner_user.id)

            response = client.post(
                "/api/decks",
                json={
                    "player_id": other_player.id,
                    "name": "Illegal Deck",
                    "commander": "Talrand, Sky Summoner",
                },
            )

            self.assertEqual(response.status_code, 403)
            self.assertEqual(response.get_json()["error"], "Forbidden")

    def test_update_permissions_for_own_vs_other_deck(self):
        with app.app.app_context():
            owner_user, owner_player, _, other_player = self._seed_users_players()
            own_deck = app.Deck(
                name="Own Deck",
                commander="Talrand, Sky Summoner",
                player_id=owner_player.id,
            )
            other_deck = app.Deck(
                name="Other Deck",
                commander="Admiral Beckett Brass",
                player_id=other_player.id,
            )
            app.db.session.add_all([own_deck, other_deck])
            app.db.session.commit()

            client = self._auth_client_for(owner_user.id)
            with patch("app.resolve_commander_metadata", side_effect=self._fake_commander_metadata):
                own_resp = client.patch(
                    f"/api/decks/{own_deck.id}",
                    json={"name": "Own Deck Updated"},
                )

            self.assertEqual(own_resp.status_code, 200)
            self.assertEqual(own_resp.get_json()["name"], "Own Deck Updated")

            forbidden_resp = client.patch(
                f"/api/decks/{other_deck.id}",
                json={"name": "Should Fail"},
            )
            self.assertEqual(forbidden_resp.status_code, 403)
            self.assertEqual(forbidden_resp.get_json()["error"], "Forbidden")

    def test_validation_errors_for_required_fields_and_invalid_body(self):
        with app.app.app_context():
            owner_user, owner_player, _, _ = self._seed_users_players()
            deck = app.Deck(
                name="Deck To Validate",
                commander="Talrand, Sky Summoner",
                player_id=owner_player.id,
            )
            app.db.session.add(deck)
            app.db.session.commit()

            client = self._auth_client_for(owner_user.id)

            invalid_json_resp = client.post(
                "/api/decks",
                data="not-json",
                content_type="application/json",
            )
            self.assertEqual(invalid_json_resp.status_code, 400)
            self.assertEqual(invalid_json_resp.get_json()["error"], "Invalid request body")

            missing_name_resp = client.post(
                "/api/decks",
                json={"commander": "Talrand, Sky Summoner"},
            )
            self.assertEqual(missing_name_resp.status_code, 400)
            self.assertEqual(missing_name_resp.get_json()["error"], "name is required")

            invalid_import_resp = client.post(
                "/api/decks",
                json={
                    "name": "Invalid Import",
                    "commander": "Talrand, Sky Summoner",
                    "raw_import": ["not", "a", "string"],
                },
            )
            self.assertEqual(invalid_import_resp.status_code, 400)

            patch_empty_name_resp = client.patch(
                f"/api/decks/{deck.id}",
                json={"name": "   "},
            )
            self.assertEqual(patch_empty_name_resp.status_code, 400)
            self.assertEqual(patch_empty_name_resp.get_json()["error"], "name must be a non-empty string")

            put_missing_name_resp = client.put(
                f"/api/decks/{deck.id}",
                json={"commander": "Talrand, Sky Summoner"},
            )
            self.assertEqual(put_missing_name_resp.status_code, 400)
            self.assertEqual(put_missing_name_resp.get_json()["error"], "name is required")

    def test_import_text_persists_decklist_and_tags(self):
        with app.app.app_context():
            owner_user, owner_player, _, _ = self._seed_users_players()
            client = self._auth_client_for(owner_user.id)

            def fake_lookup(name: str):
                if name == "Court of Grace":
                    return {"oracle_text": "When Court of Grace enters the battlefield, you become the monarch."}
                if name == "Arcane Signet":
                    return {"oracle_text": "{T}: Add one mana of any color."}
                return {"oracle_text": ""}

            with patch("app.resolve_commander_metadata", side_effect=self._fake_commander_metadata), patch(
                "app.scryfall_named_exact", side_effect=fake_lookup
            ):
                response = client.post(
                    "/api/decks",
                    json={
                        "name": "Imported Deck",
                        "commander": "Talrand, Sky Summoner",
                        "decklist_text": "1 Talrand, Sky Summoner\n1 Court of Grace\n1 Arcane Signet",
                    },
                )

            self.assertEqual(response.status_code, 201)
            deck = app.Deck.query.filter_by(name="Imported Deck", player_id=owner_player.id).one()
            self.assertIn("Court of Grace", deck.decklist_text)
            tags = json.loads(deck.tags_json)
            self.assertTrue(tags.get("monarch"))


if __name__ == "__main__":
    unittest.main()
