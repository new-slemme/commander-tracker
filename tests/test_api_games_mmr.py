import json
import os
import tempfile
import unittest

import app


class ApiGamesMmrTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmpdir = tempfile.TemporaryDirectory()
        cls._db_path = os.path.join(cls._tmpdir.name, "api_games_mmr.sqlite")
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

    def _seed(self):
        admin = app.User(
            username="admin-user",
            display_name="Admin User",
            password_hash="hashed",
            is_active=True,
            is_admin=True,
        )
        app.db.session.add(admin)

        pod = app.Pod(name=app.DEFAULT_POD_NAME, slug=app.DEFAULT_POD_SLUG, is_active=True)
        app.db.session.add(pod)

        p1 = app.Player(name="Player 1")
        p2 = app.Player(name="Player 2")
        app.db.session.add_all([p1, p2])
        app.db.session.flush()

        d1 = app.Deck(name="Deck 1", commander="Cmd 1", player_id=p1.id)
        d2 = app.Deck(name="Deck 2", commander="Cmd 2", player_id=p2.id)
        app.db.session.add_all([d1, d2])
        app.db.session.flush()
        app.db.session.add_all([
            app.PodMembership(pod_id=pod.id, player_id=p1.id),
            app.PodMembership(pod_id=pod.id, player_id=p2.id),
        ])
        app.db.session.commit()

        return admin.id, p1.id, p2.id, d1.id, d2.id

    def test_post_api_games_updates_mmr(self):
        with app.app.app_context():
            admin_id, p1_id, p2_id, d1_id, d2_id = self._seed()
            client = app.app.test_client()
            with client.session_transaction() as session:
                session["user_id"] = admin_id

            response = client.post(
                "/api/games",
                json={
                    "winner_id": p1_id,
                    "participants": [
                        {"player_id": p1_id, "deck_id": d1_id, "seat_position": 1},
                        {"player_id": p2_id, "deck_id": d2_id, "seat_position": 2},
                    ],
                },
            )

            self.assertEqual(response.status_code, 200, response.get_data(as_text=True))

            # Deck MMR updated: winner gained, loser lost.
            d1 = app.db.session.get(app.Deck, d1_id)
            d2 = app.db.session.get(app.Deck, d2_id)
            self.assertGreater(d1.mmr, app.STARTING_MMR)
            self.assertLess(d2.mmr, app.STARTING_MMR)

            # Per-deck history appended.
            h1 = json.loads(d1.mmr_history_json)
            h2 = json.loads(d2.mmr_history_json)
            self.assertEqual(len(h1), 1)
            self.assertEqual(len(h2), 1)
            self.assertGreater(h1[0]["delta"], 0)
            self.assertLess(h2[0]["delta"], 0)
            self.assertEqual(h1[0]["mmr_after"], d1.mmr)

            # Game.mmr_deltas_json written for both decks.
            game = app.Game.query.order_by(app.Game.id.desc()).first()
            self.assertIsNotNone(game.mmr_deltas_json)
            deltas = {row["deck_id"]: row["delta"] for row in json.loads(game.mmr_deltas_json)}
            self.assertEqual(set(deltas), {d1_id, d2_id})
            self.assertGreater(deltas[d1_id], 0)
            self.assertLess(deltas[d2_id], 0)

            # Participant rows carry their mmr_delta.
            parts = app.GameParticipant.query.filter_by(game_id=game.id).all()
            self.assertTrue(all(part.mmr_delta is not None for part in parts))


if __name__ == "__main__":
    unittest.main()
