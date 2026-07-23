"""Phase 2 tests: consolidated dashboard — Leaderboard and Decks in Form."""
import json
import os
import re
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


def _make_game(winner_player, participants, pod=None):
    """participants: list of (player, deck) tuples."""
    g = Game(
        date=datetime.utcnow(),
        winner_id=winner_player.id,
        win_type="combat",
        pod_id=pod.id if pod else None,
    )
    db.session.add(g)
    db.session.flush()
    for p, d in participants:
        gp = GameParticipant(game_id=g.id, player_id=p.id, deck_id=d.id)
        db.session.add(gp)
    db.session.commit()
    return g


class DeckFormLabelTests(unittest.TestCase):
    """deck_form_label() must return deterministic, evidence-backed labels."""

    def test_helper_exists(self):
        import app as app_module
        self.assertTrue(callable(getattr(app_module, "deck_form_label", None)))

    def test_new_label_for_zero_uses(self):
        import app as app_module
        label = app_module.deck_form_label({"uses": 0, "wins": 0, "winrate": 0.0, "mmr": 1000})
        self.assertEqual(label, "New")

    def test_no_label_for_sparse_data(self):
        import app as app_module
        label = app_module.deck_form_label({"uses": 1, "wins": 1, "winrate": 100.0, "mmr": 1000})
        self.assertIsNone(label)

    def test_leader_label_for_top_mmr(self):
        import app as app_module
        # The helper accepts is_top_mmr kwarg
        label = app_module.deck_form_label(
            {"uses": 5, "wins": 4, "winrate": 80.0, "mmr": 1400},
            is_top_mmr=True,
        )
        self.assertEqual(label, "Leader")

    def test_hot_label_for_high_winrate(self):
        import app as app_module
        label = app_module.deck_form_label({"uses": 5, "wins": 4, "winrate": 75.0, "mmr": 1200})
        self.assertEqual(label, "Hot")

    def test_slumping_label_for_low_winrate_with_enough_games(self):
        import app as app_module
        label = app_module.deck_form_label({"uses": 5, "wins": 0, "winrate": 0.0, "mmr": 900})
        self.assertEqual(label, "Slumping")


class LeaderboardIntegrationTests(unittest.TestCase):
    """Homepage must render one Leaderboard section with required DOM contract."""

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
            u, _ = _make_user("dash_admin", is_admin=True)
            db.session.commit()
            uid = u.id
        with client.session_transaction() as sess:
            sess["user_id"] = uid
            sess["username"] = "dash_admin"
            sess["is_admin"] = True

    def test_homepage_has_exactly_one_leaderboard_section(self):
        with flask_app.test_client() as client:
            self._login_admin(client)
            resp = client.get("/")
            self.assertEqual(resp.status_code, 200)
            html = resp.data.decode()
            matches = re.findall(r'id="leaderboard"', html)
            self.assertEqual(len(matches), 1, "Expected exactly one #leaderboard element")

    def test_homepage_has_exactly_one_decks_in_form_section(self):
        with flask_app.test_client() as client:
            self._login_admin(client)
            resp = client.get("/")
            self.assertEqual(resp.status_code, 200)
            html = resp.data.decode()
            matches = re.findall(r'id="decks-in-form"', html)
            self.assertEqual(len(matches), 1, "Expected exactly one #decks-in-form element")

    def test_top_players_pyramid_is_gone(self):
        """The old Top Players pyramid heading must not appear after consolidation."""
        with flask_app.test_client() as client:
            self._login_admin(client)
            resp = client.get("/")
            html = resp.data.decode()
            self.assertNotIn("Top Players", html,
                             "Old 'Top Players' heading still present — not consolidated")

    def test_deck_spotlight_is_gone(self):
        """The old Deck Spotlight heading must not appear after consolidation."""
        with flask_app.test_client() as client:
            self._login_admin(client)
            resp = client.get("/")
            html = resp.data.decode()
            self.assertNotIn("Deck Spotlight", html,
                             "Old 'Deck Spotlight' heading still present — not consolidated")

    def test_top_decks_heading_is_gone(self):
        """The old Top Decks heading must not appear after consolidation."""
        with flask_app.test_client() as client:
            self._login_admin(client)
            resp = client.get("/")
            html = resp.data.decode()
            self.assertNotIn("Top Decks", html,
                             "Old 'Top Decks' heading still present — not consolidated")

    def test_player_rows_have_entity_attributes(self):
        """Player elements must carry data-entity-type and data-entity-id."""
        with flask_app.test_client() as client:
            with flask_app.app_context():
                u, _ = _make_user("entity_player", is_admin=True)
                db.session.commit()
                uid = u.id
                pid = u.player.id
            with client.session_transaction() as sess:
                sess["user_id"] = uid
                sess["username"] = "entity_player"
                sess["is_admin"] = True
            resp = client.get("/")
            html = resp.data.decode()
            self.assertIn('data-entity-type="player"', html)
            self.assertIn(f'data-entity-id="{pid}"', html)

    def test_metric_selector_buttons_present(self):
        """Leaderboard must have metric selector buttons for wins, MMR, and win rate."""
        with flask_app.test_client() as client:
            self._login_admin(client)
            resp = client.get("/")
            html = resp.data.decode()
            self.assertIn('data-metric="wins"', html)
            self.assertIn('data-metric="mmr"', html)
            self.assertIn('data-metric="winrate"', html)

    def test_player_rows_have_metric_data_attributes(self):
        """Leaderboard rows need data-wins, data-winrate, data-mmr for client-side sort."""
        with flask_app.test_client() as client:
            with flask_app.app_context():
                u, _ = _make_user("metric_player", is_admin=True)
                db.session.commit()
                uid = u.id
            with client.session_transaction() as sess:
                sess["user_id"] = uid
                sess["username"] = "metric_player"
                sess["is_admin"] = True
            resp = client.get("/")
            html = resp.data.decode()
            self.assertIn("data-wins=", html)
            self.assertIn("data-winrate=", html)
            self.assertIn("data-mmr=", html)

    def test_deck_tiles_have_entity_attributes(self):
        """Deck tiles in Decks in Form must carry entity attributes."""
        with flask_app.test_client() as client:
            with flask_app.app_context():
                u, p = _make_user("deck_entity_user", is_admin=True)
                d = _make_deck(p, "Featured Deck")
                db.session.commit()
                uid = u.id
                deck_id = d.id
            with client.session_transaction() as sess:
                sess["user_id"] = uid
                sess["username"] = "deck_entity_user"
                sess["is_admin"] = True
            resp = client.get("/")
            html = resp.data.decode()
            self.assertIn('data-entity-type="deck"', html)
            self.assertIn(f'data-entity-id="{deck_id}"', html)

    def test_empty_leaderboard_shows_action(self):
        """Empty state must offer a next action, not a dead end."""
        with flask_app.test_client() as client:
            self._login_admin(client)
            resp = client.get("/")
            html = resp.data.decode()
            # Even with no players, there should be a link to add a player or start a game
            self.assertTrue(
                "/players" in html or "/play_game" in html,
                "Empty leaderboard does not link to a next action",
            )


class DashboardScopeTests(unittest.TestCase):
    """Scope=pod vs scope=all filters dashboard data correctly."""

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

    def test_scope_all_page_loads(self):
        with flask_app.test_client() as client:
            with flask_app.app_context():
                u, _ = _make_user("scope_all_user", is_admin=True)
                db.session.commit()
                uid = u.id
            with client.session_transaction() as sess:
                sess["user_id"] = uid
                sess["is_admin"] = True
            resp = client.get("/?scope=all")
            self.assertEqual(resp.status_code, 200)

    def test_scope_pod_page_loads(self):
        with flask_app.test_client() as client:
            with flask_app.app_context():
                u, _ = _make_user("scope_pod_user", is_admin=True)
                db.session.commit()
                uid = u.id
            with client.session_transaction() as sess:
                sess["user_id"] = uid
                sess["is_admin"] = True
            resp = client.get("/?scope=pod")
            self.assertEqual(resp.status_code, 200)


if __name__ == "__main__":
    unittest.main()
