"""Phase 7 tests: entity-drawer cross-linking from game detail rows and palette."""
import json
import os
import unittest
from datetime import datetime
from werkzeug.security import generate_password_hash

from app import app as flask_app, db, User, Player, Deck, Game, GameParticipant, Pod


def _make_user(username, is_admin=True):
    u = User(
        username=username,
        display_name=username.title(),
        password_hash=generate_password_hash("pass"),
        is_active=True,
        is_admin=is_admin,
    )
    db.session.add(u)
    db.session.flush()
    p = Player(name=u.display_name, user_id=u.id)
    db.session.add(p)
    db.session.flush()
    return u, p


def _make_deck(player, name="Test Deck", commander="Test Cmdr"):
    d = Deck(name=name, commander=commander, player_id=player.id)
    db.session.add(d)
    db.session.flush()
    return d


def _make_game(winner, pod, parts):
    g = Game(
        date=datetime(2026, 1, 1, 20, 0),
        winner_id=winner.id,
        pod_id=pod.id,
        win_type="combat",
        ending_turn=7,
    )
    db.session.add(g)
    db.session.flush()
    for player, deck in parts:
        gp = GameParticipant(
            game_id=g.id, player_id=player.id, deck_id=deck.id, flags_json="{}"
        )
        db.session.add(gp)
    db.session.flush()
    return g


def _login(client, uid):
    with client.session_transaction() as sess:
        sess["user_id"] = uid
        sess["is_admin"] = True


class GameDetailCrosslinkTests(unittest.TestCase):
    """Player and deck links in expanded game rows must carry entity-drawer attributes."""

    @classmethod
    def setUpClass(cls):
        flask_app.config["TESTING"] = True
        with flask_app.app_context():
            db.create_all()

    @classmethod
    def tearDownClass(cls):
        with flask_app.app_context():
            db.drop_all()

    def setUp(self):
        with flask_app.app_context():
            db.session.rollback()
            for tbl in reversed(db.metadata.sorted_tables):
                db.session.execute(tbl.delete())
            db.session.commit()

    def _setup(self):
        with flask_app.app_context():
            u, p = _make_user("alice")
            u2, p2 = _make_user("bob")
            d = _make_deck(p, "Slivers", "The First Sliver")
            d2 = _make_deck(p2, "Pirates", "Malcolm")
            pod = Pod(name="Pod", slug="pod")
            db.session.add(pod)
            db.session.flush()
            _make_game(p, pod, [(p, d), (p2, d2)])
            db.session.commit()
            return u.id, p.id, d.id, p2.id, d2.id

    def test_player_link_in_detail_row_has_entity_type(self):
        uid, pid, did, *_ = self._setup()
        with flask_app.test_client() as client:
            _login(client, uid)
            html = client.get("/").data.decode()
            self.assertIn('data-entity-type="player"', html,
                          "Player link in game-detail-row must carry data-entity-type=player")

    def test_player_link_in_detail_row_has_entity_id(self):
        uid, pid, did, *_ = self._setup()
        with flask_app.test_client() as client:
            _login(client, uid)
            html = client.get("/").data.decode()
            self.assertIn(f'data-entity-id="{pid}"', html,
                          "Player link in game-detail-row must carry data-entity-id")

    def test_deck_link_in_detail_row_has_entity_type(self):
        uid, pid, did, *_ = self._setup()
        with flask_app.test_client() as client:
            _login(client, uid)
            html = client.get("/").data.decode()
            self.assertIn('data-entity-type="deck"', html,
                          "Deck link in game-detail-row must carry data-entity-type=deck")

    def test_deck_link_in_detail_row_has_entity_id(self):
        uid, pid, did, *_ = self._setup()
        with flask_app.test_client() as client:
            _login(client, uid)
            html = client.get("/").data.decode()
            self.assertIn(f'data-entity-id="{did}"', html,
                          "Deck link in game-detail-row must carry data-entity-id")


class PaletteEntityDataTests(unittest.TestCase):
    """/api/search must return player and deck IDs so the palette can render entity attributes."""

    @classmethod
    def setUpClass(cls):
        flask_app.config["TESTING"] = True
        with flask_app.app_context():
            db.create_all()

    @classmethod
    def tearDownClass(cls):
        with flask_app.app_context():
            db.drop_all()

    def setUp(self):
        with flask_app.app_context():
            db.session.rollback()
            for tbl in reversed(db.metadata.sorted_tables):
                db.session.execute(tbl.delete())
            db.session.commit()

    def _setup(self):
        with flask_app.app_context():
            u, p = _make_user("alice")
            d = _make_deck(p, "Slivers", "The First Sliver")
            db.session.commit()
            return u.id, p.id, d.id

    def test_search_player_result_includes_id(self):
        uid, pid, did = self._setup()
        with flask_app.test_client() as client:
            _login(client, uid)
            resp = client.get("/api/search?q=alice")
            data = json.loads(resp.data)
            self.assertTrue(len(data["players"]) > 0)
            self.assertIn("id", data["players"][0],
                          "Player search result must include id field for entity-drawer")

    def test_search_deck_result_includes_id(self):
        uid, pid, did = self._setup()
        with flask_app.test_client() as client:
            _login(client, uid)
            resp = client.get("/api/search?q=sliver")
            data = json.loads(resp.data)
            self.assertTrue(len(data["decks"]) > 0)
            self.assertIn("id", data["decks"][0],
                          "Deck search result must include id field for entity-drawer")


if __name__ == "__main__":
    unittest.main()
