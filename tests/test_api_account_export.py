"""Phase 6: GDPR account export over JSON.

The web route already builds this payload; the API needed the same thing without a
browser download. Two things are worth pinning:

  * it must be the *same* payload, so the two cannot drift and a user does not get a
    different answer depending on which client they asked from;
  * it must contain this account's data and nobody else's -- an export is exactly the
    place where over-fetching turns into a data leak.
"""
import json
import unittest
from datetime import datetime

from werkzeug.security import generate_password_hash

import app as app_module
from app import (
    app as flask_app, db, User, Player, Pod, Deck, Game, GameParticipant,
)

PASSWORD = "StrongPass123"


class AccountExportApiTests(unittest.TestCase):

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

        self.me = self._user("exporter")
        self.pod = self._pod("My Pod", self.me)
        self.stranger = self._user("stranger")
        self._pod("Their Pod", self.stranger)

        self.my_deck = self._deck("My Atraxa", self.me)
        self._deck("Their Krenko", self.stranger)
        self._game(self.me, self.my_deck)

        self.client = self._signed_in(self.me)

    def tearDown(self):
        db.session.remove()
        self.ctx.pop()

    def _user(self, username):
        user = User(
            username=username,
            display_name=username.title(),
            email=f"{username}@example.test",
            password_hash=generate_password_hash(PASSWORD),
            is_active=True,
            approved_at=datetime.utcnow(),
            email_verified_at=datetime.utcnow(),
        )
        db.session.add(user)
        db.session.flush()
        user.player = Player(name=user.display_name, user_id=user.id)
        db.session.commit()
        return user

    def _pod(self, name, owner):
        pod = Pod(name=name, slug=app_module.unique_pod_slug(name),
                  owner_user_id=owner.id, is_active=True)
        db.session.add(pod)
        db.session.flush()
        app_module.ensure_membership(pod.id, owner.player.id, role="podmaster")
        db.session.commit()
        return pod

    def _deck(self, name, owner):
        deck = Deck(name=name, commander="Cmdr", commander_name="Cmdr",
                    player_id=owner.player.id, decklist_text="1 Sol Ring")
        db.session.add(deck)
        db.session.commit()
        return deck

    def _game(self, owner, deck):
        game = Game(pod_id=self.pod.id, date=datetime.utcnow(), winner_id=owner.player.id)
        db.session.add(game)
        db.session.flush()
        db.session.add(GameParticipant(
            game_id=game.id, player_id=owner.player.id, deck_id=deck.id,
            seat_position=1, mmr_delta=11,
        ))
        db.session.commit()
        return game

    def _signed_in(self, user):
        client = flask_app.test_client()
        self.assertEqual(
            client.post("/api/login", json={"username": user.username, "password": PASSWORD}).status_code,
            200,
        )
        return client

    # ----- contract -------------------------------------------------------

    def test_export_requires_a_session(self):
        response = flask_app.test_client().get("/api/account/export")
        self.assertEqual(response.status_code, 401)

    def test_export_returns_json_not_a_download(self):
        response = self.client.get("/api/account/export")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "application/json")
        # A client saves this itself; a browser Content-Disposition would be wrong here.
        self.assertIsNone(response.headers.get("Content-Disposition"))

    def test_export_carries_the_account_and_its_data(self):
        body = self.client.get("/api/account/export").get_json()
        self.assertEqual(body["account"]["username"], "exporter")
        self.assertEqual(body["account"]["email"], "exporter@example.test")
        self.assertIn("exported_at", body)
        self.assertEqual([d["name"] for d in body["decks"]], ["My Atraxa"])
        self.assertEqual(len(body["games"]), 1)
        self.assertEqual(body["games"][0]["mmr_delta"], 11)

    def test_export_contains_nobody_elses_data(self):
        raw = self.client.get("/api/account/export").get_data(as_text=True)
        self.assertNotIn("Their Krenko", raw)
        self.assertNotIn("stranger@example.test", raw)
        self.assertNotIn("Their Pod", raw)

    def test_the_api_and_the_web_export_agree(self):
        # Same builder, so the two cannot drift into telling a user different things.
        api_body = self.client.get("/api/account/export").get_json()
        web_body = json.loads(self.client.get("/account/export").get_data(as_text=True))
        api_body.pop("exported_at")
        web_body.pop("exported_at")
        self.assertEqual(api_body, web_body)

    def test_an_account_with_no_player_still_exports(self):
        # Freshly approved accounts can exist without a Player row.
        orphan = User(
            username="orphan", display_name="Orphan", email="orphan@example.test",
            password_hash=generate_password_hash(PASSWORD), is_active=True,
            approved_at=datetime.utcnow(),
        )
        db.session.add(orphan)
        db.session.commit()
        body = self._signed_in(orphan).get("/api/account/export").get_json()
        self.assertEqual(body["decks"], [])
        self.assertEqual(body["games"], [])
        self.assertEqual(body["account"]["username"], "orphan")


if __name__ == "__main__":
    unittest.main()
