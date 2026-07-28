import json
import os
import tempfile
import unittest

import app


class LoadActiveGameLifeHistoryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmpdir = tempfile.TemporaryDirectory()
        cls._db_path = os.path.join(cls._tmpdir.name, "end_game_life_history.sqlite")
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

    def test_returns_empty_for_missing_token(self):
        with app.app.app_context():
            self.assertEqual(app.load_active_game_life_history(None), {})
            self.assertEqual(app.load_active_game_life_history("nope"), {})

    def test_filters_short_and_malformed_histories(self):
        with app.app.app_context():
            host = app.User(
                username="host", display_name="Host", password_hash="x", is_active=True
            )
            app.db.session.add(host)
            app.db.session.flush()
            state = {
                "life_history": {
                    "1": [[100, 40], [110, 35]],
                    "2": [[100, 40]],
                    "3": "corrupt",
                }
            }
            app.db.session.add(
                app.ActiveGame(
                    token="tok-history",
                    host_user_id=host.id,
                    participants_json="[]",
                    state_json=json.dumps(state),
                    created_at=app.datetime.utcnow(),
                    updated_at=app.datetime.utcnow(),
                )
            )
            app.db.session.commit()

            result = app.load_active_game_life_history("tok-history")
            self.assertEqual(result, {"1": [[100, 40], [110, 35]]})


class EndGamePersistsLifeHistoryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmpdir = tempfile.TemporaryDirectory()
        cls._db_path = os.path.join(cls._tmpdir.name, "end_game_persist.sqlite")
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

    def test_end_game_copies_life_history_onto_participants(self):
        with app.app.app_context():
            admin = app.User(
                username="admin",
                display_name="Admin",
                password_hash="x",
                is_active=True,
                is_admin=True,
            )
            pod = app.Pod(name="Test Pod", slug="test-pod", is_active=True)
            p1 = app.Player(name="P One")
            p2 = app.Player(name="P Two")
            app.db.session.add_all([admin, pod, p1, p2])
            app.db.session.flush()
            d1 = app.Deck(name="Deck One", commander="A", player_id=p1.id)
            d2 = app.Deck(name="Deck Two", commander="B", player_id=p2.id)
            app.db.session.add_all([d1, d2])
            app.db.session.flush()

            token = "tok-endgame"
            state = {
                "life_history": {
                    str(p1.id): [[100, 40], [130, 30], [150, 25]],
                    str(p2.id): [[100, 40]],
                }
            }
            app.db.session.add(
                app.ActiveGame(
                    token=token,
                    host_user_id=admin.id,
                    participants_json="[]",
                    state_json=json.dumps(state),
                    created_at=app.datetime.utcnow(),
                    updated_at=app.datetime.utcnow(),
                )
            )
            app.db.session.commit()

            participants = [
                {"player_id": p1.id, "deck_id": d1.id, "seat_position": 1},
                {"player_id": p2.id, "deck_id": d2.id, "seat_position": 2},
            ]

            client = app.app.test_client()
            with client.session_transaction() as sess:
                sess["user_id"] = admin.id
                sess["game_participants"] = participants
                sess["game_token"] = token
                sess["_csrf_token"] = "test-csrf-token"

            response = client.post(
                "/end_game",
                data={"winner": str(p1.id), "_csrf_token": "test-csrf-token"},
            )
            self.assertEqual(response.status_code, 302)

            row1 = app.GameParticipant.query.filter_by(player_id=p1.id).first()
            row2 = app.GameParticipant.query.filter_by(player_id=p2.id).first()
            self.assertEqual(
                json.loads(row1.life_history_json),
                [[100, 40], [130, 30], [150, 25]],
            )
            self.assertIsNone(row2.life_history_json)


if __name__ == "__main__":
    unittest.main()
