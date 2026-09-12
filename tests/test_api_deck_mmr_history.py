"""Phase 5: a deck's MMR history has to reach the client to be charted.

`mmr` and `mmr_tier` were already served; the per-game history behind them lived only
in `Deck.mmr_history_json` and never left the server, so a client could show where a
deck stands but not how it got there.

The stored rows carry more than a chart needs (game id, delta, timestamp) and are
unbounded -- one entry per game, forever. These tests pin the trimmed, capped shape
the API exposes rather than handing over the raw column.
"""
import json
import unittest
from datetime import datetime, timedelta

from werkzeug.security import generate_password_hash

import app as app_module
from app import app as flask_app, db, User, Player, Pod, Deck


class DeckMmrHistoryApiTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        flask_app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
        flask_app.config["TESTING"] = True
        app_module.limiter.enabled = False
        with flask_app.app_context():
            db.create_all()

    @classmethod
    def tearDownClass(cls):
        with flask_app.app_context():
            db.drop_all()

    def setUp(self):
        self.ctx = flask_app.app_context()
        self.ctx.push()
        db.session.query(Deck).delete()
        db.session.query(Pod).delete()
        db.session.query(Player).delete()
        db.session.query(User).delete()
        db.session.commit()

        self.user = User(
            username="mmr_owner",
            display_name="Owner",
            password_hash=generate_password_hash("pass"),
            is_active=True,
            approved_at=datetime.utcnow(),
        )
        db.session.add(self.user)
        db.session.flush()
        self.user.player = Player(name="Owner", user_id=self.user.id)
        db.session.commit()

        self.client = flask_app.test_client()
        with self.client.session_transaction() as sess:
            sess["user_id"] = self.user.id

    def tearDown(self):
        db.session.remove()
        self.ctx.pop()

    def _deck(self, mmr=1000, history=None):
        deck = Deck(
            name="Atraxa",
            commander="Atraxa, Praetors' Voice",
            commander_name="Atraxa, Praetors' Voice",
            player_id=self.user.player.id,
            mmr=mmr,
            mmr_history_json=json.dumps(history or []),
        )
        db.session.add(deck)
        db.session.commit()
        return deck

    def _entries(self, count, start_mmr=1000):
        base = datetime(2026, 1, 1)
        rows = []
        mmr = start_mmr
        for i in range(count):
            mmr += 10
            rows.append({
                "game_id": i + 1,
                "delta": 10,
                "mmr_after": mmr,
                "date": (base + timedelta(days=i)).isoformat(),
            })
        return rows

    def test_detail_exposes_the_history_as_points(self):
        deck = self._deck(history=self._entries(3))
        body = self.client.get(f"/api/decks/{deck.id}").get_json()
        self.assertEqual(
            body["mmr_history"],
            [
                {"mmr": 1010, "delta": 10, "game_id": 1},
                {"mmr": 1020, "delta": 10, "game_id": 2},
                {"mmr": 1030, "delta": 10, "game_id": 3},
            ],
        )

    def test_a_deck_that_never_played_has_an_empty_history(self):
        deck = self._deck()
        self.assertEqual(self.client.get(f"/api/decks/{deck.id}").get_json()["mmr_history"], [])

    def test_history_is_capped_to_the_most_recent(self):
        cap = app_module.MAX_MMR_HISTORY_POINTS
        deck = self._deck(history=self._entries(cap + 25))
        points = self.client.get(f"/api/decks/{deck.id}").get_json()["mmr_history"]
        self.assertEqual(len(points), cap)
        # The newest entries are the ones worth charting.
        self.assertEqual(points[-1]["game_id"], cap + 25)

    def test_a_corrupt_history_column_does_not_break_the_endpoint(self):
        deck = self._deck()
        deck.mmr_history_json = "{not json"
        db.session.commit()
        response = self.client.get(f"/api/decks/{deck.id}")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["mmr_history"], [])

    def test_malformed_entries_are_skipped_not_fatal(self):
        deck = self._deck(history=[
            {"mmr_after": 1010, "delta": 10, "game_id": 1},
            "nonsense",
            {"no_mmr": True},
            {"mmr_after": 1020, "delta": 10, "game_id": 2},
        ])
        points = self.client.get(f"/api/decks/{deck.id}").get_json()["mmr_history"]
        self.assertEqual([p["mmr"] for p in points], [1010, 1020])

    def test_the_summary_list_stays_lean(self):
        # The deck list renders many tiles; per-deck history would bloat it for a
        # chart only the detail screen draws.
        self._deck(history=self._entries(3))
        # /api/decks returns a bare array, not an envelope.
        decks = self.client.get("/api/decks").get_json()
        self.assertNotIn("mmr_history", decks[0])
        self.assertIn("mmr", decks[0])
        self.assertIn("mmr_tier", decks[0])

    def test_tier_boundaries_match_the_web(self):
        for mmr, expected in ((1300, "S"), (1299, "A"), (1200, "A"), (1199, "B"),
                              (1100, "B"), (1099, "C"), (950, "C"), (949, "D")):
            with self.subTest(mmr=mmr):
                self.assertEqual(app_module.mmr_tier(mmr), expected)


if __name__ == "__main__":
    unittest.main()
