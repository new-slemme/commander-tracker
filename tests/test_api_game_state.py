import json
import os
import tempfile
import unittest

import app


class ApiGameStateCardStateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmpdir = tempfile.TemporaryDirectory()
        cls._db_path = os.path.join(cls._tmpdir.name, "api_game_state.sqlite")
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

    def _create_active_game(self):
        host_user = app.User(
            username="host-user",
            display_name="Host User",
            password_hash="hashed",
            is_active=True,
        )
        app.db.session.add(host_user)

        p1 = app.Player(name="Player 1")
        p2 = app.Player(name="Player 2")
        app.db.session.add_all([p1, p2])
        app.db.session.flush()

        token = "tok123"
        state = {
            "life": {str(p1.id): 40, str(p2.id): 40},
            "flags": {},
            "card_state": {
                str(p1.id): {
                    "counters": {"shield": 1},
                    "commander_damage": {str(p1.id): 2},
                    "statuses": {"hexproof": True},
                }
            },
            "version": 0,
        }
        active_game = app.ActiveGame(
            token=token,
            host_user_id=host_user.id,
            participants_json=json.dumps([
                {"player_id": p1.id, "player_name": p1.name},
                {"player_id": p2.id, "player_name": p2.name},
            ]),
            state_json=json.dumps(state),
            created_at=app.datetime.utcnow(),
            updated_at=app.datetime.utcnow(),
        )
        app.db.session.add(active_game)
        app.db.session.commit()

        return token, p1.id, p2.id

    def test_remote_commander_damage_update_persists_and_merges(self):
        with app.app.app_context():
            token, p1_id, p2_id = self._create_active_game()
            client = app.app.test_client()
            with client.session_transaction() as session:
                session[f"game_join_{token}"] = p1_id

            response = client.post(
                f"/api/game/{token}/state",
                json={
                    "player_id": p1_id,
                    "card_state": {
                        "commander_damage": {str(p2_id): 120},
                    },
                },
            )

            self.assertEqual(response.status_code, 200)
            updated = response.get_json()
            self.assertEqual(
                updated["card_state"][str(p1_id)]["commander_damage"],
                {str(p1_id): 2, str(p2_id): 99},
            )
            self.assertEqual(updated["card_state"][str(p1_id)]["counters"], {"shield": 1})
            self.assertEqual(updated["card_state"][str(p1_id)]["statuses"], {"hexproof": True})

    def test_invalid_commander_damage_source_ids_are_ignored(self):
        with app.app.app_context():
            token, p1_id, p2_id = self._create_active_game()
            client = app.app.test_client()
            with client.session_transaction() as session:
                session[f"game_join_{token}"] = p1_id

            response = client.post(
                f"/api/game/{token}/state",
                json={
                    "player_id": p1_id,
                    "card_state": {
                        "commander_damage": {
                            "not-a-player": 5,
                            "99999": 7,
                            str(p2_id): 4,
                        },
                    },
                },
            )

            self.assertEqual(response.status_code, 200)
            updated = response.get_json()
            self.assertEqual(
                updated["card_state"][str(p1_id)]["commander_damage"],
                {str(p1_id): 2, str(p2_id): 4},
            )

    def test_non_claimed_player_update_is_forbidden(self):
        with app.app.app_context():
            token, p1_id, p2_id = self._create_active_game()
            client = app.app.test_client()
            with client.session_transaction() as session:
                session[f"game_join_{token}"] = p1_id

            response = client.post(
                f"/api/game/{token}/state",
                json={
                    "player_id": p2_id,
                    "card_state": {"commander_damage": {str(p1_id): 3}},
                },
            )

            self.assertEqual(response.status_code, 403)


if __name__ == "__main__":
    unittest.main()
