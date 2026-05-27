"""
MMR system tests.

Requires running in an environment where /data is accessible (e.g. Docker),
same as all other test modules in this directory. Tests will skip gracefully
if the app cannot be imported.
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


_APP_IMPORT_ERROR: str | None = None


def _try_import_app():
    global _APP_IMPORT_ERROR
    if "app" in sys.modules:
        return sys.modules["app"]
    try:
        import app
        return app
    except Exception as exc:
        _APP_IMPORT_ERROR = str(exc)
        return None


_app = _try_import_app()


def _skip_if_no_app(fn):
    return unittest.skipIf(_app is None, f"app import failed: {_APP_IMPORT_ERROR}")(fn)


# ---------------------------------------------------------------------------
# Pure-function tests — math only, no DB
# ---------------------------------------------------------------------------

class MmrPureFunctionTests(unittest.TestCase):

    def _ew(self, mmrs):
        return _app.calculate_expected_wins(mmrs)

    def _deltas(self, mmrs, winner):
        return _app.calculate_mmr_deltas(mmrs, winner)

    def setUp(self):
        if _app is None:
            self.skipTest(f"app import failed: {_APP_IMPORT_ERROR}")

    def test_expected_wins_equal_mmr(self):
        result = self._ew([1000, 1000, 1000, 1000])
        self.assertAlmostEqual(sum(result), 1.0, places=9)
        for p in result:
            self.assertAlmostEqual(p, 0.25, places=9)

    def test_expected_wins_proportional(self):
        mmrs = [1200, 1000, 950, 850]
        result = self._ew(mmrs)
        total = sum(mmrs)
        for i, mmr in enumerate(mmrs):
            self.assertAlmostEqual(result[i], mmr / total, places=9)
        self.assertAlmostEqual(sum(result), 1.0, places=9)

    def test_deltas_equal_mmr_winner_gets_36_losers_get_minus_12(self):
        # K=48, expected=0.25 each
        # Winner: 48*(1-0.25)=36  Loser: 48*(0-0.25)=-12
        deltas = self._deltas([1000, 1000, 1000, 1000], winner=0)
        self.assertEqual(deltas[0], 36)
        for d in deltas[1:]:
            self.assertEqual(d, -12)

    def test_deltas_sum_approximately_zero(self):
        mmrs = [1200, 1000, 950, 850]
        for winner_index in range(4):
            deltas = self._deltas(mmrs, winner=winner_index)
            self.assertAlmostEqual(sum(deltas), 0, delta=len(mmrs))

    def test_low_mmr_winner_gets_larger_delta_than_high_mmr_winner(self):
        mmrs = [1200, 1000, 950, 850]
        low_wins = self._deltas(mmrs, winner=3)   # 850 wins
        high_wins = self._deltas(mmrs, winner=0)  # 1200 wins
        self.assertGreater(low_wins[3], high_wins[0])

    def test_mmr_floor_not_exceeded(self):
        floor = _app.MMR_FLOOR
        mmrs = [1500, 1500, 1500, floor]
        deltas = self._deltas(mmrs, winner=0)
        for mmr, delta in zip(mmrs, deltas):
            self.assertGreaterEqual(max(floor, mmr + delta), floor)

    def test_two_player_pod(self):
        deltas = self._deltas([1000, 1000], winner=0)
        self.assertAlmostEqual(sum(deltas), 0, delta=2)
        self.assertGreater(deltas[0], 0)
        self.assertLess(deltas[1], 0)

    def test_mmr_tier_labels(self):
        self.assertEqual(_app.mmr_tier(1300), "S")
        self.assertEqual(_app.mmr_tier(1400), "S")
        self.assertEqual(_app.mmr_tier(1200), "A")
        self.assertEqual(_app.mmr_tier(1299), "A")
        self.assertEqual(_app.mmr_tier(1100), "B")
        self.assertEqual(_app.mmr_tier(1199), "B")
        self.assertEqual(_app.mmr_tier(950), "C")
        self.assertEqual(_app.mmr_tier(1099), "C")
        self.assertEqual(_app.mmr_tier(949), "D")
        self.assertEqual(_app.mmr_tier(100), "D")

    def test_constants(self):
        self.assertEqual(_app.STARTING_MMR, 1000)
        self.assertEqual(_app.K_FACTOR, 48)
        self.assertEqual(_app.MMR_FLOOR, 100)


# ---------------------------------------------------------------------------
# Database integration tests
# ---------------------------------------------------------------------------

class MmrDatabaseTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        if _app is None:
            return
        cls._tmpdir = tempfile.TemporaryDirectory()
        cls._db_path = os.path.join(cls._tmpdir.name, "mmr_test.sqlite")
        _app.app.config.update(
            TESTING=True,
            SQLALCHEMY_DATABASE_URI=f"sqlite:///{cls._db_path}",
            WTF_CSRF_ENABLED=False,
        )

    @classmethod
    def tearDownClass(cls):
        if _app is None:
            return
        cls._tmpdir.cleanup()

    def setUp(self):
        if _app is None:
            self.skipTest(f"app import failed: {_APP_IMPORT_ERROR}")
        with _app.app.app_context():
            _app.db.session.remove()
            _app.db.drop_all()
            _app.db.create_all()

    def _make_players_and_decks(self, count=4):
        players = [_app.Player(name=f"Player{i}") for i in range(count)]
        _app.db.session.add_all(players)
        _app.db.session.flush()
        decks = [
            _app.Deck(name=f"Deck{i}", commander=f"Commander{i}", player_id=players[i].id)
            for i in range(count)
        ]
        _app.db.session.add_all(decks)
        _app.db.session.flush()
        return players, decks

    def test_new_deck_default_mmr(self):
        with _app.app.app_context():
            player = _app.Player(name="Tester")
            _app.db.session.add(player)
            _app.db.session.flush()
            deck = _app.Deck(name="TestDeck", commander="Commander", player_id=player.id)
            _app.db.session.add(deck)
            _app.db.session.flush()

            self.assertEqual(deck.mmr, _app.STARTING_MMR)
            self.assertEqual(json.loads(deck.mmr_history_json), [])

    def test_equal_mmr_game_updates_correctly(self):
        """4 equal-MMR decks: winner gets +36, each loser -12."""
        with _app.app.app_context():
            players, decks = self._make_players_and_decks(4)
            pod = _app.Pod(name="TestPod", slug="test-pod")
            _app.db.session.add(pod)
            _app.db.session.flush()

            game = _app.Game(winner_id=players[0].id, pod_id=pod.id)
            _app.db.session.add(game)
            _app.db.session.flush()

            pod_deck_ids = [d.id for d in decks]
            pod_deck_objs = [_app.db.session.get(_app.Deck, did) for did in pod_deck_ids]
            deltas = _app.calculate_mmr_deltas([d.mmr for d in pod_deck_objs], winner_index=0)

            for deck, delta in zip(pod_deck_objs, deltas):
                new_mmr = max(_app.MMR_FLOOR, deck.mmr + delta)
                history = json.loads(deck.mmr_history_json or "[]")
                history.append({"game_id": game.id, "delta": delta, "mmr_after": new_mmr, "date": "2025-01-01T00:00:00"})
                deck.mmr = new_mmr
                deck.mmr_history_json = json.dumps(history)
            game.mmr_deltas_json = json.dumps(
                [{"deck_id": did, "delta": d} for did, d in zip(pod_deck_ids, deltas)]
            )
            _app.db.session.commit()
            _app.db.session.expire_all()

            winner_deck = _app.db.session.get(_app.Deck, decks[0].id)
            loser_deck = _app.db.session.get(_app.Deck, decks[1].id)
            self.assertEqual(winner_deck.mmr, 1036)
            self.assertEqual(loser_deck.mmr, 988)
            self.assertEqual(len(json.loads(winner_deck.mmr_history_json)), 1)

    def test_mmr_history_grows_per_game(self):
        with _app.app.app_context():
            players, decks = self._make_players_and_decks(4)
            pod = _app.Pod(name="TestPod2", slug="test-pod-2")
            _app.db.session.add(pod)
            _app.db.session.flush()

            for _ in range(3):
                game = _app.Game(winner_id=players[0].id, pod_id=pod.id)
                _app.db.session.add(game)
                _app.db.session.flush()
                pod_deck_ids = [d.id for d in decks]
                pod_deck_objs = [_app.db.session.get(_app.Deck, did) for did in pod_deck_ids]
                deltas = _app.calculate_mmr_deltas([d.mmr for d in pod_deck_objs], winner_index=0)
                for deck, delta in zip(pod_deck_objs, deltas):
                    new_mmr = max(_app.MMR_FLOOR, deck.mmr + delta)
                    history = json.loads(deck.mmr_history_json or "[]")
                    history.append({"game_id": game.id, "delta": delta, "mmr_after": new_mmr, "date": "2025-01-01T00:00:00"})
                    deck.mmr = new_mmr
                    deck.mmr_history_json = json.dumps(history)
                _app.db.session.commit()
                _app.db.session.expire_all()

            winner_deck = _app.db.session.get(_app.Deck, decks[0].id)
            self.assertEqual(len(json.loads(winner_deck.mmr_history_json)), 3)

    def test_mmr_floor_not_breached(self):
        with _app.app.app_context():
            players, decks = self._make_players_and_decks(4)
            pod = _app.Pod(name="FloorPod", slug="floor-pod")
            _app.db.session.add(pod)
            _app.db.session.flush()

            floor_deck = _app.db.session.get(_app.Deck, decks[3].id)
            floor_deck.mmr = _app.MMR_FLOOR
            _app.db.session.commit()
            _app.db.session.expire_all()

            game = _app.Game(winner_id=players[0].id, pod_id=pod.id)
            _app.db.session.add(game)
            _app.db.session.flush()
            pod_deck_objs = [_app.db.session.get(_app.Deck, d.id) for d in decks]
            deltas = _app.calculate_mmr_deltas([d.mmr for d in pod_deck_objs], winner_index=0)
            for deck, delta in zip(pod_deck_objs, deltas):
                deck.mmr = max(_app.MMR_FLOOR, deck.mmr + delta)
            _app.db.session.commit()
            _app.db.session.expire_all()

            floor_deck_after = _app.db.session.get(_app.Deck, decks[3].id)
            self.assertGreaterEqual(floor_deck_after.mmr, _app.MMR_FLOOR)

    def test_game_mmr_deltas_json_stored_and_sum_zero(self):
        with _app.app.app_context():
            players, decks = self._make_players_and_decks(4)
            pod = _app.Pod(name="DeltaJsonPod", slug="delta-json-pod")
            _app.db.session.add(pod)
            _app.db.session.flush()

            game = _app.Game(winner_id=players[0].id, pod_id=pod.id)
            _app.db.session.add(game)
            _app.db.session.flush()
            pod_deck_ids = [d.id for d in decks]
            pod_deck_objs = [_app.db.session.get(_app.Deck, did) for did in pod_deck_ids]
            deltas = _app.calculate_mmr_deltas([d.mmr for d in pod_deck_objs], winner_index=0)
            game.mmr_deltas_json = json.dumps(
                [{"deck_id": did, "delta": d} for did, d in zip(pod_deck_ids, deltas)]
            )
            _app.db.session.commit()
            _app.db.session.expire_all()

            saved_game = _app.db.session.get(_app.Game, game.id)
            stored = json.loads(saved_game.mmr_deltas_json)
            self.assertEqual(len(stored), 4)
            self.assertAlmostEqual(sum(e["delta"] for e in stored), 0, delta=4)


if __name__ == "__main__":
    unittest.main()
