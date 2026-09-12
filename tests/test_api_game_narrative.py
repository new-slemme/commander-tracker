"""Phase 5: the game detail API should carry what the web's narrative view shows.

The web game page shows, per seat, whether the monarch was taken and how much poison
was dealt, plus the MMR the game moved. The JSON endpoint carried the salt flags but
not those, so the app could not show the same story -- and a rematch needs the seats
and decks to prefill from.
"""
import json
import unittest
from datetime import datetime

from werkzeug.security import generate_password_hash

import app as app_module
from app import (
    app as flask_app, db, User, Player, Pod, Deck, Game, GameParticipant,
)


class GameNarrativeApiTests(unittest.TestCase):

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
        for model in (GameParticipant, Game, Deck, Pod, Player, User):
            db.session.query(model).delete()
        db.session.commit()

        self.user = User(
            username="narrator",
            display_name="Narrator",
            password_hash=generate_password_hash("pass"),
            is_active=True,
            is_admin=True,
            approved_at=datetime.utcnow(),
        )
        db.session.add(self.user)
        db.session.flush()
        self.user.player = Player(name="Narrator", user_id=self.user.id)
        db.session.commit()

        self.pod = Pod(name="Pod", slug="pod", owner_user_id=self.user.id, is_active=True)
        db.session.add(self.pod)
        db.session.commit()

        self.client = flask_app.test_client()
        with self.client.session_transaction() as sess:
            sess["user_id"] = self.user.id

    def tearDown(self):
        db.session.remove()
        self.ctx.pop()

    def _game_with_seat(self, flags=None, mmr_delta=None, tags=None):
        deck = Deck(
            name="Atraxa",
            commander="Atraxa, Praetors' Voice",
            commander_name="Atraxa, Praetors' Voice",
            player_id=self.user.player.id,
            tags_json=json.dumps(tags or {}),
        )
        db.session.add(deck)
        db.session.flush()
        game = Game(pod_id=self.pod.id, date=datetime.utcnow(), winner_id=self.user.player.id)
        db.session.add(game)
        db.session.flush()
        part = GameParticipant(
            game_id=game.id,
            player_id=self.user.player.id,
            deck_id=deck.id,
            seat_position=1,
            flags_json=json.dumps(flags or {}),
            mmr_delta=mmr_delta,
        )
        db.session.add(part)
        db.session.commit()
        return game

    def test_a_seat_reports_the_monarch_and_poison_it_dealt(self):
        game = self._game_with_seat(flags={"monarch": True, "poison": 7})
        seat = self.client.get(f"/api/games/{game.id}").get_json()["participants"][0]
        self.assertTrue(seat["monarch"])
        self.assertEqual(seat["poison"], 7)

    def test_an_ordinary_seat_reports_neither(self):
        game = self._game_with_seat()
        seat = self.client.get(f"/api/games/{game.id}").get_json()["participants"][0]
        self.assertFalse(seat["monarch"])
        self.assertEqual(seat["poison"], 0)

    def test_poison_is_clamped_to_a_lethal_ten(self):
        game = self._game_with_seat(flags={"poison": 99})
        seat = self.client.get(f"/api/games/{game.id}").get_json()["participants"][0]
        self.assertEqual(seat["poison"], 10)

    def test_a_seat_reports_the_rating_the_game_moved(self):
        game = self._game_with_seat(mmr_delta=13)
        seat = self.client.get(f"/api/games/{game.id}").get_json()["participants"][0]
        self.assertEqual(seat["mmr_delta"], 13)

    def test_an_unrated_seat_reports_no_delta_rather_than_zero(self):
        # Zero would read as "played and gained nothing", which is a different fact.
        game = self._game_with_seat()
        seat = self.client.get(f"/api/games/{game.id}").get_json()["participants"][0]
        self.assertIsNone(seat["mmr_delta"])

    def test_a_seat_carries_the_decks_mechanics_for_chips(self):
        game = self._game_with_seat(tags={"monarch": True, "proliferate": True})
        seat = self.client.get(f"/api/games/{game.id}").get_json()["participants"][0]
        self.assertTrue(seat["mechanics"]["monarch"])
        # poison keys off proliferate too, matching derive_deck_mechanics
        self.assertTrue(seat["mechanics"]["poison"])
        self.assertFalse(seat["mechanics"]["energy"])

    def test_corrupt_flags_do_not_break_the_endpoint(self):
        game = self._game_with_seat()
        part = GameParticipant.query.filter_by(game_id=game.id).one()
        part.flags_json = "{not json"
        db.session.commit()
        response = self.client.get(f"/api/games/{game.id}")
        self.assertEqual(response.status_code, 200)
        seat = response.get_json()["participants"][0]
        self.assertFalse(seat["monarch"])
        self.assertEqual(seat["poison"], 0)


if __name__ == "__main__":
    unittest.main()
