"""Phase 4 tests: expandable game activity and compare tray."""
import json
import os
import re
import unittest
from datetime import datetime
from werkzeug.security import generate_password_hash

from app import app as flask_app, db, User, Player, Deck, Game, GameParticipant


def _make_user(username, is_admin=False):
    u = User(
        username=username,
        display_name=username.replace("_", " ").title(),
        password_hash=generate_password_hash("pass"),
        is_active=True,
        is_admin=is_admin,
    )
    db.session.add(u)
    db.session.flush()
    p = Player(name=u.display_name, user_id=u.id)
    db.session.add(p)
    db.session.flush()
    u.player = p
    return u, p


def _make_deck(player, name="Test Deck"):
    d = Deck(
        name=name,
        commander="Test Commander",
        player_id=player.id,
        retired=False,
        planned=False,
    )
    db.session.add(d)
    db.session.flush()
    return d


def _make_game(winner_player, participants):
    g = Game(
        date=datetime.utcnow(),
        winner_id=winner_player.id,
        win_type="combat",
    )
    db.session.add(g)
    db.session.flush()
    for p, d in participants:
        gp = GameParticipant(game_id=g.id, player_id=p.id, deck_id=d.id)
        db.session.add(gp)
    db.session.commit()
    return g


def _login(client, user_id, is_admin=False):
    with client.session_transaction() as sess:
        sess["user_id"] = user_id
        sess["is_admin"] = is_admin


class ExpandableGameTests(unittest.TestCase):
    """Recent game tiles must be expandable with aria-expanded controls."""

    @classmethod
    def setUpClass(cls):
        flask_app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
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
            u, p = _make_user("expand_admin", is_admin=True)
            u2, p2 = _make_user("expand_opp")
            d = _make_deck(p, "Expand Deck")
            d2 = _make_deck(p2, "Opp Deck")
            _make_game(p, [(p, d), (p2, d2)])
            db.session.commit()
            return u.id

    def test_game_tiles_have_expand_button(self):
        """Each game tile must have a dedicated expand/collapse button with aria-expanded."""
        uid = self._setup()
        with flask_app.test_client() as client:
            _login(client, uid, is_admin=True)
            resp = client.get("/")
            html = resp.data.decode()
            self.assertIn("aria-expanded=", html,
                          "No aria-expanded found — expand buttons missing from game tiles")

    def test_game_tiles_have_detail_region(self):
        """Each game tile must have a hidden detail region toggled by the expand button."""
        uid = self._setup()
        with flask_app.test_client() as client:
            _login(client, uid, is_admin=True)
            resp = client.get("/")
            html = resp.data.decode()
            self.assertIn("game-detail-region", html,
                          "No .game-detail-region found — expandable detail missing")

    def test_game_link_still_navigable(self):
        """The game title/link must remain a valid href even with expand controls."""
        uid = self._setup()
        with flask_app.test_client() as client:
            _login(client, uid, is_admin=True)
            resp = client.get("/")
            html = resp.data.decode()
            # /games/<id> link must exist
            self.assertRegex(html, r'href="/games/\d+"',
                             "Game full-page link missing from recent game tile")


class CompareTrayTests(unittest.TestCase):
    """The base template must include a comparison tray shell."""

    @classmethod
    def setUpClass(cls):
        flask_app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
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

    def _login_admin(self, client):
        with flask_app.app_context():
            u, _ = _make_user("tray_admin", is_admin=True)
            db.session.commit()
            uid = u.id
        _login(client, uid, is_admin=True)

    def test_compare_tray_shell_present(self):
        with flask_app.test_client() as client:
            self._login_admin(client)
            resp = client.get("/")
            html = resp.data.decode()
            self.assertIn('id="compare-tray"', html,
                          "Compare tray shell #compare-tray missing from base template")

    def test_compare_tray_has_compare_link(self):
        with flask_app.test_client() as client:
            self._login_admin(client)
            resp = client.get("/")
            html = resp.data.decode()
            self.assertIn('id="compare-tray-link"', html,
                          "Compare tray must have a #compare-tray-link to /compare")

    def test_compare_controls_on_leaderboard(self):
        """Player rows in the leaderboard must expose a compare-add control."""
        with flask_app.test_client() as client:
            with flask_app.app_context():
                u, p = _make_user("cmp_player", is_admin=True)
                db.session.commit()
                uid, pid = u.id, p.id
            _login(client, uid, is_admin=True)
            resp = client.get("/")
            html = resp.data.decode()
            self.assertIn("data-compare-player=", html,
                          "No data-compare-player attributes found on leaderboard")


class CompareRouteTests(unittest.TestCase):
    """Existing /compare route must still work with query params a= and b=."""

    @classmethod
    def setUpClass(cls):
        flask_app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
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

    def test_compare_route_loads(self):
        with flask_app.test_client() as client:
            with flask_app.app_context():
                u, p = _make_user("cmp_route_user", is_admin=True)
                u2, p2 = _make_user("cmp_route_opp")
                db.session.commit()
                uid, pid, pid2 = u.id, p.id, p2.id
            _login(client, uid, is_admin=True)
            resp = client.get(f"/compare?a={pid}&b={pid2}")
            self.assertIn(resp.status_code, (200, 302),
                          f"Compare route failed: {resp.status_code}")


if __name__ == "__main__":
    unittest.main()
