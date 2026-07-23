"""Phase 6 tests: enriched game detail rows as narrative."""
import json
import os
import unittest
from datetime import datetime
from werkzeug.security import generate_password_hash

os.environ.setdefault("COMMANDER_DB_URI", "sqlite:///:memory:")
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


def _make_deck(player, name="Test Deck", commander="Test Commander"):
    d = Deck(name=name, commander=commander, player_id=player.id)
    db.session.add(d)
    db.session.flush()
    return d


def _make_game(winner, pod, win_type="combat", ending_turn=8, salt=3,
               mmr_deltas=None, participants=None):
    """participants: list of (player, deck, flags_json_str)"""
    g = Game(
        date=datetime(2026, 1, 1, 20, 0),
        winner_id=winner.id,
        pod_id=pod.id,
        win_type=win_type,
        ending_turn=ending_turn,
        salt_rating=salt,
        mmr_deltas_json=json.dumps(mmr_deltas or []),
    )
    db.session.add(g)
    db.session.flush()
    for player, deck, flags in (participants or []):
        gp = GameParticipant(
            game_id=g.id,
            player_id=player.id,
            deck_id=deck.id,
            flags_json=flags,
        )
        db.session.add(gp)
    db.session.flush()
    return g


def _login_admin(client, uid):
    with client.session_transaction() as sess:
        sess["user_id"] = uid
        sess["is_admin"] = True


class GameDetailNarrativeTests(unittest.TestCase):
    """Expanded game detail rows must show commander, win type, and active mechanics."""

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

    def _make_scenario(self):
        with flask_app.app_context():
            u, p = _make_user("alice")
            u2, p2 = _make_user("bob")
            d = _make_deck(p, "Pirates", commander="Malcolm Lee Waya")
            d2 = _make_deck(p2, "Slivers", commander="The First Sliver")
            pod = Pod(name="TestPod", slug="test-pod")
            db.session.add(pod)
            db.session.flush()
            flags_winner = json.dumps({"monarch": True, "poison": 0, "mana_fucked": False})
            flags_loser = json.dumps({"monarch": False, "poison": 3, "mana_fucked": True})
            g = _make_game(
                winner=p,
                pod=pod,
                win_type="combo",
                ending_turn=11,
                salt=4,
                mmr_deltas=[
                    {"deck_id": d.id, "delta": 36},
                    {"deck_id": d2.id, "delta": -12},
                ],
                participants=[
                    (p, d, flags_winner),
                    (p2, d2, flags_loser),
                ],
            )
            db.session.commit()
            return u.id, p.id, g.id

    def test_commander_name_in_game_detail_row(self):
        uid, _, gid = self._make_scenario()
        with flask_app.test_client() as client:
            _login_admin(client, uid)
            html = client.get("/").data.decode()
            self.assertIn("Malcolm", html,
                          "Commander name must appear in game detail rows")

    def test_win_type_shown_in_detail(self):
        uid, _, gid = self._make_scenario()
        with flask_app.test_client() as client:
            _login_admin(client, uid)
            html = client.get("/").data.decode()
            self.assertIn("combo", html.lower(),
                          "Win type must appear in game detail region")

    def test_active_mechanic_tag_shown(self):
        """Mechanics set to true in flags_json must render as tags in the detail row."""
        uid, _, gid = self._make_scenario()
        with flask_app.test_client() as client:
            _login_admin(client, uid)
            html = client.get("/").data.decode()
            self.assertIn("Monarch", html,
                          "Active mechanic 'Monarch' must appear in the detail row")

    def test_poison_count_shown_when_nonzero(self):
        uid, _, gid = self._make_scenario()
        with flask_app.test_client() as client:
            _login_admin(client, uid)
            html = client.get("/").data.decode()
            self.assertIn("Poison", html,
                          "Poison counter must appear when nonzero")

    def test_mana_fucked_shown(self):
        uid, _, gid = self._make_scenario()
        with flask_app.test_client() as client:
            _login_admin(client, uid)
            html = client.get("/").data.decode()
            self.assertIn("Mana", html,
                          "Mana Fucked flag must appear in the detail row")

    def test_salt_rating_shown_in_game_header(self):
        uid, _, gid = self._make_scenario()
        with flask_app.test_client() as client:
            _login_admin(client, uid)
            html = client.get("/").data.decode()
            self.assertIn("salt", html.lower(),
                          "Salt rating must appear in the game header or detail")

    def test_rematch_link_present(self):
        """Each game must offer a rematch link targeting /play_game with participant pre-fill."""
        uid, _, gid = self._make_scenario()
        with flask_app.test_client() as client:
            _login_admin(client, uid)
            html = client.get("/").data.decode()
            self.assertIn("rematch", html.lower(),
                          "A rematch link must appear in the game detail region")


if __name__ == "__main__":
    unittest.main()
