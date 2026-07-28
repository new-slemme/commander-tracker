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
        non_host_user = app.User(
            username="non-host-user",
            display_name="Non Host User",
            password_hash="hashed",
            is_active=True,
        )
        app.db.session.add_all([host_user, non_host_user])

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

        return token, p1.id, p2.id, non_host_user.id

    def test_remote_commander_damage_update_persists_and_merges(self):
        with app.app.app_context():
            token, p1_id, p2_id, non_host_user_id = self._create_active_game()
            client = app.app.test_client()
            with client.session_transaction() as session:
                session["user_id"] = non_host_user_id
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
            token, p1_id, p2_id, non_host_user_id = self._create_active_game()
            client = app.app.test_client()
            with client.session_transaction() as session:
                session["user_id"] = non_host_user_id
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
            token, p1_id, p2_id, non_host_user_id = self._create_active_game()
            client = app.app.test_client()
            with client.session_transaction() as session:
                session["user_id"] = non_host_user_id
                session[f"game_join_{token}"] = p1_id

            response = client.post(
                f"/api/game/{token}/state",
                json={
                    "player_id": p2_id,
                    "card_state": {"commander_damage": {str(p1_id): 3}},
                },
            )

            self.assertEqual(response.status_code, 403)

    def _create_active_game_with_active_player(self):
        host_user = app.User(
            username="host-user2",
            display_name="Host User2",
            password_hash="hashed",
            is_active=True,
        )
        non_host_user = app.User(
            username="non-host-user2",
            display_name="Non Host User2",
            password_hash="hashed",
            is_active=True,
        )
        app.db.session.add_all([host_user, non_host_user])

        p1 = app.Player(name="Player 1")
        p2 = app.Player(name="Player 2")
        app.db.session.add_all([p1, p2])
        app.db.session.flush()

        token = "tok456"
        state = {
            "life": {str(p1.id): 40, str(p2.id): 40},
            "flags": {},
            "card_state": {},
            "version": 0,
            "turn": 1,
            "active_player_id": p1.id,
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

        return token, host_user.id, non_host_user.id, p1.id, p2.id

    def test_non_host_player_can_pass_turn(self):
        with app.app.app_context():
            token, host_user_id, non_host_user_id, p1_id, p2_id = self._create_active_game_with_active_player()
            client = app.app.test_client()
            with client.session_transaction() as session:
                session["user_id"] = non_host_user_id
                session[f"game_join_{token}"] = p1_id

            response = client.post(
                f"/api/game/{token}/state",
                json={"player_id": p1_id, "pass_turn": True},
            )

            self.assertEqual(response.status_code, 200)
            updated = response.get_json()
            self.assertEqual(updated["active_player_id"], p2_id)

    def test_host_player_can_pass_turn(self):
        with app.app.app_context():
            token, host_user_id, non_host_user_id, p1_id, p2_id = self._create_active_game_with_active_player()
            client = app.app.test_client()
            with client.session_transaction() as session:
                # Host is also playing as p1
                session["user_id"] = host_user_id
                session[f"game_join_{token}"] = p1_id

            response = client.post(
                f"/api/game/{token}/state",
                json={"player_id": p1_id, "pass_turn": True},
            )

            self.assertEqual(response.status_code, 200)
            updated = response.get_json()
            self.assertEqual(updated["active_player_id"], p2_id)

    def test_life_update_appends_life_history_sample(self):
        with app.app.app_context():
            token, p1_id, p2_id, non_host_user_id = self._create_active_game()
            client = app.app.test_client()
            with client.session_transaction() as session:
                session["user_id"] = non_host_user_id
                session[f"game_join_{token}"] = p1_id

            response = client.post(
                f"/api/game/{token}/state",
                json={"player_id": p1_id, "life": 35},
            )

            self.assertEqual(response.status_code, 200)
            updated = response.get_json()
            history = updated["life_history"][str(p1_id)]
            self.assertEqual(len(history), 1)
            timestamp, life = history[-1]
            self.assertEqual(life, 35)
            self.assertIsInstance(timestamp, int)

    def test_life_delta_appends_life_history_sample(self):
        with app.app.app_context():
            token, p1_id, p2_id, non_host_user_id = self._create_active_game()
            client = app.app.test_client()
            with client.session_transaction() as session:
                session["user_id"] = non_host_user_id
                session[f"game_join_{token}"] = p1_id

            response = client.post(
                f"/api/game/{token}/state",
                json={"player_id": p1_id, "life_delta": -5},
            )

            self.assertEqual(response.status_code, 200)
            updated = response.get_json()
            history = updated["life_history"][str(p1_id)]
            self.assertEqual(history[-1][1], 35)

    def test_unchanged_life_does_not_append_history_sample(self):
        with app.app.app_context():
            token, p1_id, p2_id, non_host_user_id = self._create_active_game()
            client = app.app.test_client()
            with client.session_transaction() as session:
                session["user_id"] = non_host_user_id
                session[f"game_join_{token}"] = p1_id

            client.post(f"/api/game/{token}/state", json={"player_id": p1_id, "life": 35})
            response = client.post(
                f"/api/game/{token}/state",
                json={"player_id": p1_id, "life": 35},
            )

            self.assertEqual(response.status_code, 200)
            updated = response.get_json()
            self.assertEqual(len(updated["life_history"][str(p1_id)]), 1)

    def test_life_history_is_capped(self):
        with app.app.app_context():
            token, p1_id, p2_id, non_host_user_id = self._create_active_game()
            active_game = app.ActiveGame.query.filter_by(token=token).first()
            state = json.loads(active_game.state_json)
            state["life_history"] = {
                str(p1_id): [[i, 40] for i in range(app.MAX_LIFE_HISTORY_SAMPLES)]
            }
            active_game.state_json = json.dumps(state)
            app.db.session.commit()

            client = app.app.test_client()
            with client.session_transaction() as session:
                session["user_id"] = non_host_user_id
                session[f"game_join_{token}"] = p1_id

            response = client.post(
                f"/api/game/{token}/state",
                json={"player_id": p1_id, "life": 12},
            )

            self.assertEqual(response.status_code, 200)
            history = response.get_json()["life_history"][str(p1_id)]
            self.assertEqual(len(history), app.MAX_LIFE_HISTORY_SAMPLES)
            self.assertEqual(history[-1][1], 12)


if __name__ == "__main__":
    unittest.main()
