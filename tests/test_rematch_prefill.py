"""Phase 8 tests: rematch link pre-population and play_game prefill."""
import json
import os
import unittest
from werkzeug.security import generate_password_hash

from app import app as flask_app, db, User, Player, Deck


def _make_user(username):
    u = User(
        username=username,
        display_name=username.title(),
        password_hash=generate_password_hash("pass"),
        is_active=True,
        is_admin=True,
    )
    db.session.add(u)
    db.session.flush()
    p = Player(name=u.display_name, user_id=u.id)
    db.session.add(p)
    db.session.flush()
    return u, p


def _make_deck(player, name="Deck"):
    d = Deck(name=name, commander="Test Commander", player_id=player.id)
    db.session.add(d)
    db.session.flush()
    return d


def _login(client, uid):
    with client.session_transaction() as sess:
        sess["user_id"] = uid
        sess["is_admin"] = True


class RematchPrefillTests(unittest.TestCase):
    """play_game route must pre-select players when p1..pN params are present."""

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
            _make_deck(p, "Slivers")
            _make_deck(p2, "Pirates")
            db.session.commit()
            return u.id, p.id, p2.id

    def test_play_game_with_prefill_returns_200(self):
        uid, pid1, pid2 = self._setup()
        with flask_app.test_client() as client:
            _login(client, uid)
            resp = client.get(f"/play_game?p1={pid1}&p2={pid2}")
            self.assertEqual(resp.status_code, 200)

    def test_play_game_prefill_selected_attribute_in_html(self):
        uid, pid1, pid2 = self._setup()
        with flask_app.test_client() as client:
            _login(client, uid)
            html = client.get(f"/play_game?p1={pid1}&p2={pid2}").data.decode()
            self.assertIn(f'value="{pid1}" selected', html,
                          "Player 1 option must be pre-selected via HTML selected attribute")

    def test_play_game_prefill_injects_js_array(self):
        uid, pid1, pid2 = self._setup()
        with flask_app.test_client() as client:
            _login(client, uid)
            html = client.get(f"/play_game?p1={pid1}&p2={pid2}").data.decode()
            self.assertIn("prefillPlayerIds", html,
                          "prefillPlayerIds JS variable must be injected into play_game template")

    def test_play_game_without_prefill_returns_200(self):
        uid, pid1, pid2 = self._setup()
        with flask_app.test_client() as client:
            _login(client, uid)
            resp = client.get("/play_game")
            self.assertEqual(resp.status_code, 200)

    def test_play_game_invalid_player_param_ignored(self):
        uid, pid1, pid2 = self._setup()
        with flask_app.test_client() as client:
            _login(client, uid)
            resp = client.get("/play_game?p1=abc&p2=999999")
            self.assertEqual(resp.status_code, 200,
                             "Non-numeric or unknown player IDs must not crash the route")


class RematchLinkTests(unittest.TestCase):
    """Index page rematch link must include player ID params for all game participants."""

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
        from app import Game, GameParticipant, Pod
        from datetime import datetime
        with flask_app.app_context():
            u, p = _make_user("alice")
            u2, p2 = _make_user("bob")
            d = _make_deck(p, "Slivers")
            d2 = _make_deck(p2, "Pirates")
            pod = Pod(name="Pod", slug="pod")
            db.session.add(pod)
            db.session.flush()
            g = Game(
                date=datetime(2026, 1, 1),
                winner_id=p.id,
                pod_id=pod.id,
                win_type="combat",
            )
            db.session.add(g)
            db.session.flush()
            for player, deck in [(p, d), (p2, d2)]:
                gp = GameParticipant(game_id=g.id, player_id=player.id, deck_id=deck.id, flags_json="{}")
                db.session.add(gp)
            db.session.commit()
            return u.id, p.id, p2.id

    def test_rematch_link_contains_player_ids(self):
        uid, pid1, pid2 = self._setup()
        with flask_app.test_client() as client:
            _login(client, uid)
            html = client.get("/").data.decode()
            self.assertIn(f"p1={pid1}", html,
                          "Rematch link must include p1=<player_id>")
            self.assertIn(f"p2={pid2}", html,
                          "Rematch link must include p2=<player_id>")


if __name__ == "__main__":
    unittest.main()
