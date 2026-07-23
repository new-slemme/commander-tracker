import json
import os
import tempfile
import threading
import time
import unittest
from unittest import mock

import app

# Widen the window between the handler's state read and its write so the
# lost-update race is reliably reproducible. This is a per-thread sleep (not a
# rendezvous), so it does NOT deadlock the fixed, serialized implementation --
# it merely makes each critical section last long enough that a second,
# unsynchronised writer would overlap it.
READ_WRITE_GAP_SECONDS = 0.25


class ApiGameStateConcurrencyTests(unittest.TestCase):
    """Regression tests for TASK-R11 / ADR-014: the read-modify-write of
    ActiveGame.state_json in api_game_state POST must be atomic so that two
    concurrent updates from the same base version both survive.

    A pure in-memory (":memory:") DB will not reproduce cross-connection
    locking, so a temp file DB is used. The engine carries the same
    busy_timeout / WAL configuration the app sets in production.
    """

    @classmethod
    def setUpClass(cls):
        cls._tmpdir = tempfile.TemporaryDirectory()
        cls._db_path = os.path.join(cls._tmpdir.name, "api_game_state_concurrency.sqlite")
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

    def _create_active_game(self, initial_life=40):
        host_user = app.User(
            username="host-user-conc",
            display_name="Host User Conc",
            password_hash="hashed",
            is_active=True,
        )
        app.db.session.add(host_user)

        p1 = app.Player(name="Player 1")
        p2 = app.Player(name="Player 2")
        app.db.session.add_all([p1, p2])
        app.db.session.flush()

        token = "conc-tok"
        state = {
            "life": {str(p1.id): initial_life, str(p2.id): initial_life},
            "flags": {},
            "card_state": {},
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

        return token, host_user.id, p1.id, p2.id

    def _host_client(self, host_user_id):
        client = app.app.test_client()
        with client.session_transaction() as session:
            session["user_id"] = host_user_id
        return client

    def _run_concurrent_posts(self, token, host_user_id, payload_a, payload_b):
        """Fire two POSTs from separate threads, gated by a barrier so both
        threads enter the handler (and read the base state) before either
        commits. Returns (status_a, status_b)."""
        barrier = threading.Barrier(2)
        results = {}

        original_loads = app.json.loads

        def slow_loads(s, *args, **kwargs):
            parsed = original_loads(s, *args, **kwargs)
            # Only delay on the game-state blob (it carries "version"), not on
            # the participants list or any other json.loads call.
            if isinstance(s, str) and '"version"' in s:
                time.sleep(READ_WRITE_GAP_SECONDS)
            return parsed

        def worker(name, payload):
            client = self._host_client(host_user_id)
            barrier.wait()
            resp = client.post(f"/api/game/{token}/state", json=payload)
            results[name] = resp.status_code

        with mock.patch.object(app.json, "loads", side_effect=slow_loads):
            t_a = threading.Thread(target=worker, args=("a", payload_a))
            t_b = threading.Thread(target=worker, args=("b", payload_b))
            t_a.start()
            t_b.start()
            t_a.join()
            t_b.join()
        return results.get("a"), results.get("b")

    def _final_state(self, token):
        with app.app.app_context():
            app.db.session.remove()
            rec = app.ActiveGame.query.filter_by(token=token).first()
            return json.loads(rec.state_json)

    def test_concurrent_life_and_flag_updates_both_survive(self):
        with app.app.app_context():
            token, host_user_id, p1_id, p2_id = self._create_active_game()

        status_a, status_b = self._run_concurrent_posts(
            token,
            host_user_id,
            # A: absolute life update for player 1
            {"player_id": p1_id, "life": 33},
            # B: flag toggle for player 2
            {"player_id": p2_id, "flags": {"mana_fucked": True}},
        )

        self.assertEqual(status_a, 200)
        self.assertEqual(status_b, 200)

        final = self._final_state(token)
        # Both independent updates must survive the concurrent RMW.
        self.assertEqual(final["life"][str(p1_id)], 33)
        self.assertEqual(final["flags"].get(str(p2_id), {}).get("mana_fucked"), True)

    def test_concurrent_life_deltas_same_player_sum_correctly(self):
        with app.app.app_context():
            token, host_user_id, p1_id, p2_id = self._create_active_game(initial_life=40)

        status_a, status_b = self._run_concurrent_posts(
            token,
            host_user_id,
            {"player_id": p1_id, "life_delta": -5},
            {"player_id": p1_id, "life_delta": -3},
        )

        self.assertEqual(status_a, 200)
        self.assertEqual(status_b, 200)

        final = self._final_state(token)
        # Neither delta may be lost: 40 - 5 - 3 = 32.
        self.assertEqual(final["life"][str(p1_id)], 32)


if __name__ == "__main__":
    unittest.main()
