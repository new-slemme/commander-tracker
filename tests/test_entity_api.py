"""Phase 3 tests: additive entity API extensions for the drawer."""
import json
import os
import unittest
from datetime import datetime
from werkzeug.security import generate_password_hash

from app import app as flask_app, db, User, Player, Deck, Game, GameParticipant, Pod, PodMembership


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


class PlayerApiDrawerFieldsTests(unittest.TestCase):
    """Player detail API must expose drawer-required fields without breaking existing ones."""

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

    def test_existing_fields_preserved(self):
        """Core fields id/name/games_played/games_won/winrate/decks/recent_games must still exist."""
        with flask_app.test_client() as client:
            with flask_app.app_context():
                u, p = _make_user("pres_user", is_admin=True)
                db.session.commit()
                uid, pid = u.id, p.id
            _login(client, uid, is_admin=True)
            resp = client.get(f"/api/players/{pid}")
            self.assertEqual(resp.status_code, 200)
            data = resp.get_json()
            for field in ("id", "name", "games_played", "games_won", "winrate", "decks", "recent_games"):
                self.assertIn(field, data, f"Missing preserved field: {field}")

    def test_accent_field_present(self):
        """API must include deterministic player accent."""
        with flask_app.test_client() as client:
            with flask_app.app_context():
                u, p = _make_user("accent_user", is_admin=True)
                db.session.commit()
                uid, pid = u.id, p.id
            _login(client, uid, is_admin=True)
            resp = client.get(f"/api/players/{pid}")
            data = resp.get_json()
            self.assertIn("accent", data)
            self.assertTrue(data["accent"].startswith("var(--"), f"accent must be a CSS var: {data['accent']!r}")

    def test_full_page_url_field_present(self):
        """API must include a full_page_url for the drawer's fallback link."""
        with flask_app.test_client() as client:
            with flask_app.app_context():
                u, p = _make_user("url_user", is_admin=True)
                db.session.commit()
                uid, pid = u.id, p.id
            _login(client, uid, is_admin=True)
            resp = client.get(f"/api/players/{pid}")
            data = resp.get_json()
            self.assertIn("full_page_url", data)
            self.assertIn(str(pid), data["full_page_url"])

    def test_recent_games_bounded(self):
        """recent_games list in player API must be <= 10 entries."""
        with flask_app.test_client() as client:
            with flask_app.app_context():
                u, p = _make_user("bounded_player", is_admin=True)
                u2, p2 = _make_user("bounded_opp", is_admin=False)
                d = _make_deck(p, "Bounded Deck")
                d2 = _make_deck(p2, "Opp Deck")
                for _ in range(12):
                    _make_game(p, [(p, d), (p2, d2)])
                db.session.commit()
                uid, pid = u.id, p.id
            _login(client, uid, is_admin=True)
            resp = client.get(f"/api/players/{pid}")
            data = resp.get_json()
            self.assertLessEqual(len(data["recent_games"]), 10)

    def test_unauthenticated_returns_401(self):
        with flask_app.test_client() as client:
            with flask_app.app_context():
                _, p = _make_user("unauth_player")
                db.session.commit()
                pid = p.id
            resp = client.get(f"/api/players/{pid}")
            self.assertEqual(resp.status_code, 401)

    def test_missing_player_returns_404(self):
        with flask_app.test_client() as client:
            with flask_app.app_context():
                u, _ = _make_user("missing_admin", is_admin=True)
                db.session.commit()
                uid = u.id
            _login(client, uid, is_admin=True)
            resp = client.get("/api/players/999999")
            self.assertEqual(resp.status_code, 404)


class DeckApiDrawerFieldsTests(unittest.TestCase):
    """Deck detail API must expose drawer-required fields without breaking existing ones."""

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

    def _setup_deck(self):
        with flask_app.app_context():
            u, p = _make_user("deck_api_user", is_admin=True)
            d = _make_deck(p, "Drawer Deck")
            db.session.commit()
            return u.id, p.id, d.id

    def test_existing_fields_preserved(self):
        uid, pid, did = self._setup_deck()
        with flask_app.test_client() as client:
            _login(client, uid, is_admin=True)
            resp = client.get(f"/api/decks/{did}")
            self.assertEqual(resp.status_code, 200)
            data = resp.get_json()
            for field in ("id", "name", "commander", "player_id", "player_name",
                          "wins", "uses", "winrate", "art_url", "mmr", "recent_games"):
                self.assertIn(field, data, f"Missing preserved deck field: {field}")

    def test_owner_accent_field_present(self):
        uid, pid, did = self._setup_deck()
        with flask_app.test_client() as client:
            _login(client, uid, is_admin=True)
            resp = client.get(f"/api/decks/{did}")
            data = resp.get_json()
            self.assertIn("owner_accent", data)
            self.assertTrue(data["owner_accent"].startswith("var(--"))

    def test_full_page_url_field_present(self):
        uid, pid, did = self._setup_deck()
        with flask_app.test_client() as client:
            _login(client, uid, is_admin=True)
            resp = client.get(f"/api/decks/{did}")
            data = resp.get_json()
            self.assertIn("full_page_url", data)
            self.assertIn(str(did), data["full_page_url"])

    def test_unauthorized_deck_returns_403(self):
        uid, pid, did = self._setup_deck()
        with flask_app.test_client() as client:
            with flask_app.app_context():
                u2, _ = _make_user("deck_other_user", is_admin=False)
                db.session.commit()
                uid2 = u2.id
            _login(client, uid2, is_admin=False)
            resp = client.get(f"/api/decks/{did}")
            self.assertEqual(resp.status_code, 403)

    def test_missing_deck_returns_404(self):
        uid, pid, did = self._setup_deck()
        with flask_app.test_client() as client:
            _login(client, uid, is_admin=True)
            resp = client.get("/api/decks/999999")
            self.assertEqual(resp.status_code, 404)


class GameApiDrawerFieldsTests(unittest.TestCase):
    """Game detail API must expose drawer-required fields additively."""

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

    def _setup_game(self):
        with flask_app.app_context():
            u, p = _make_user("game_api_user", is_admin=True)
            u2, p2 = _make_user("game_api_opp", is_admin=False)
            d1 = _make_deck(p, "Game Deck A")
            d2 = _make_deck(p2, "Game Deck B")
            g = _make_game(p, [(p, d1), (p2, d2)])
            db.session.commit()
            return u.id, p.id, g.id, p2.id

    def test_existing_fields_preserved(self):
        uid, pid, gid, _ = self._setup_game()
        with flask_app.test_client() as client:
            _login(client, uid, is_admin=True)
            resp = client.get(f"/api/games/{gid}")
            self.assertEqual(resp.status_code, 200)
            data = resp.get_json()
            for field in ("id", "date", "winner", "win_type", "participants"):
                self.assertIn(field, data, f"Missing preserved game field: {field}")

    def test_full_page_url_field_present(self):
        uid, pid, gid, _ = self._setup_game()
        with flask_app.test_client() as client:
            _login(client, uid, is_admin=True)
            resp = client.get(f"/api/games/{gid}")
            data = resp.get_json()
            self.assertIn("full_page_url", data)
            self.assertIn(str(gid), data["full_page_url"])

    def test_participants_have_accent_and_player_url(self):
        """Each participant entry must include player_accent and player_url."""
        uid, pid, gid, _ = self._setup_game()
        with flask_app.test_client() as client:
            _login(client, uid, is_admin=True)
            resp = client.get(f"/api/games/{gid}")
            data = resp.get_json()
            for part in data["participants"]:
                self.assertIn("player_accent", part,
                              f"participant {part.get('player_id')} missing player_accent")
                self.assertIn("player_url", part)

    def test_missing_game_returns_404(self):
        uid, pid, gid, _ = self._setup_game()
        with flask_app.test_client() as client:
            _login(client, uid, is_admin=True)
            resp = client.get("/api/games/999999")
            self.assertEqual(resp.status_code, 404)


class DrawerShellTests(unittest.TestCase):
    """The base template must include the drawer shell markup."""

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
            u, _ = _make_user("drawer_admin", is_admin=True)
            db.session.commit()
            uid = u.id
        _login(client, uid, is_admin=True)

    def test_drawer_shell_present_in_home(self):
        with flask_app.test_client() as client:
            self._login_admin(client)
            resp = client.get("/")
            html = resp.data.decode()
            self.assertIn('id="entity-drawer"', html,
                          "Drawer shell #entity-drawer must be in the base template")

    def test_drawer_has_close_button(self):
        with flask_app.test_client() as client:
            self._login_admin(client)
            resp = client.get("/")
            html = resp.data.decode()
            self.assertIn('id="entity-drawer-close"', html)

    def test_drawer_has_content_region(self):
        with flask_app.test_client() as client:
            self._login_admin(client)
            resp = client.get("/")
            html = resp.data.decode()
            self.assertIn('id="entity-drawer-content"', html)

    def test_drawer_has_full_page_link(self):
        with flask_app.test_client() as client:
            self._login_admin(client)
            resp = client.get("/")
            html = resp.data.decode()
            self.assertIn('id="entity-drawer-full-link"', html)


if __name__ == "__main__":
    unittest.main()
