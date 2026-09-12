"""Phase 4: invites, guest players, and public recap links over JSON.

These three features are what turn a personal tracker into something a playgroup
uses, and all three hand out or publish data, so the tests are built around the
boundaries rather than the happy path:

  * invites mint a bearer token for pod membership -- who may create one, what the
    accepted bounds are, and that a revoked or spent invite fails closed.
  * guest players are records for people who never signed up and cannot consent,
    so they must stay scoped to the pod that created them.
  * a recap link publishes game data to an unauthenticated URL, with per-share
    switches for player and deck names that have to actually hide them.

Email verification gates the first and third, matching the web routes: an
unverified account can play, but cannot invite strangers or publish to the web.
"""
import unittest
from datetime import datetime, timedelta

from werkzeug.security import generate_password_hash

import app as app_module
from app import (
    app as flask_app, db, User, Player, Pod, PodMembership, PodInvite,
    Game, GameParticipant, GameShare, Deck,
)

PASSWORD = "StrongPass123"


class GrowthSurfaceTests(unittest.TestCase):

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
        for model in (GameShare, GameParticipant, Game, Deck, PodInvite,
                      PodMembership, Pod, Player, User):
            db.session.query(model).delete()
        db.session.commit()
        app_module.send_transactional_email = lambda *a, **k: True

        self.owner = self._make_user("owner", verified=True)
        self.pod = self._make_pod("Friday Crew", self.owner, role="podmaster")
        self.outsider = self._make_user("outsider", verified=True)
        self.client = self._signed_in(self.owner)

    def tearDown(self):
        db.session.remove()
        self.ctx.pop()

    # ----- fixtures -------------------------------------------------------

    def _make_user(self, username, verified=True, is_admin=False):
        user = User(
            username=username,
            display_name=username.title(),
            email=f"{username}@example.test",
            password_hash=generate_password_hash(PASSWORD),
            is_active=True,
            is_admin=is_admin,
            approved_at=datetime.utcnow(),
            email_verified_at=datetime.utcnow() if verified else None,
        )
        db.session.add(user)
        db.session.flush()
        user.player = Player(name=user.display_name, user_id=user.id)
        db.session.commit()
        return user

    def _make_pod(self, name, owner, role="podmaster"):
        pod = Pod(
            name=name,
            slug=app_module.unique_pod_slug(name),
            owner_user_id=owner.id,
            is_active=True,
        )
        db.session.add(pod)
        db.session.flush()
        app_module.ensure_membership(pod.id, owner.player.id, role=role)
        db.session.commit()
        return pod

    def _signed_in(self, user):
        client = flask_app.test_client()
        response = client.post(
            "/api/login", json={"username": user.username, "password": PASSWORD}
        )
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        return client

    def _finished_game(self):
        deck = Deck(
            name="Atraxa Superfriends",
            commander="Atraxa, Praetors' Voice",
            commander_name="Atraxa, Praetors' Voice",
            player_id=self.owner.player.id,
        )
        db.session.add(deck)
        db.session.flush()
        game = Game(pod_id=self.pod.id, date=datetime.utcnow(), winner_id=self.owner.player.id)
        db.session.add(game)
        db.session.flush()
        db.session.add(
            GameParticipant(
                game_id=game.id, player_id=self.owner.player.id,
                deck_id=deck.id, seat_position=1,
            )
        )
        db.session.commit()
        return game

    # ----- P4-1 invites ---------------------------------------------------

    def test_creating_an_invite_returns_the_link_once(self):
        response = self.client.post(f"/api/pods/{self.pod.id}/invites", json={})
        self.assertEqual(response.status_code, 201, response.get_data(as_text=True))
        body = response.get_json()
        self.assertIn("token", body)
        self.assertIn("/invite/", body["invite_url"])
        self.assertTrue(body["invite_url"].endswith(body["token"]))

        invite = PodInvite.query.filter_by(pod_id=self.pod.id).one()
        self.assertEqual(
            invite.token_hash, app_module.hash_public_token(body["token"]),
            "only the hash may be stored",
        )

    def test_invite_defaults_match_the_web_form(self):
        body = self.client.post(f"/api/pods/{self.pod.id}/invites", json={}).get_json()
        self.assertEqual(body["role"], "member")
        self.assertEqual(body["usage_limit"], 1)
        invite = PodInvite.query.one()
        self.assertAlmostEqual(
            (invite.expires_at - datetime.utcnow()).days, 6, delta=1
        )

    def test_invite_bounds_are_clamped_not_rejected(self):
        for sent, expected in ((0, 1), (99, 25)):
            with self.subTest(usage_limit=sent):
                body = self.client.post(
                    f"/api/pods/{self.pod.id}/invites", json={"usage_limit": sent}
                ).get_json()
                self.assertEqual(body["usage_limit"], expected)

    def test_an_unparseable_setting_is_a_400_not_a_crash(self):
        response = self.client.post(
            f"/api/pods/{self.pod.id}/invites", json={"expires_days": "soon"}
        )
        self.assertEqual(response.status_code, 400)

    def test_an_unknown_role_falls_back_to_member(self):
        body = self.client.post(
            f"/api/pods/{self.pod.id}/invites", json={"role": "admin"}
        ).get_json()
        self.assertEqual(body["role"], "member")

    def test_only_a_pod_manager_may_invite(self):
        outsider = self._signed_in(self.outsider)
        response = outsider.post(f"/api/pods/{self.pod.id}/invites", json={})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(PodInvite.query.count(), 0)

    def test_an_unverified_account_may_not_invite(self):
        unverified = self._make_user("unverified", verified=False)
        pod = self._make_pod("Their Pod", unverified)
        client = self._signed_in(unverified)
        response = client.post(f"/api/pods/{pod.id}/invites", json={})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.get_json()["reason"], "email_unverified")

    def test_listing_invites_never_returns_a_token(self):
        created = self.client.post(f"/api/pods/{self.pod.id}/invites", json={}).get_json()
        listing = self.client.get(f"/api/pods/{self.pod.id}/invites")
        self.assertEqual(listing.status_code, 200)
        raw = listing.get_data(as_text=True)
        self.assertNotIn(created["token"], raw, "a listing must not re-disclose the token")
        entry = listing.get_json()["invites"][0]
        self.assertEqual(entry["use_count"], 0)
        self.assertFalse(entry["revoked"])

    def test_revoking_an_invite_makes_it_unusable(self):
        created = self.client.post(f"/api/pods/{self.pod.id}/invites", json={}).get_json()
        response = self.client.post(
            f"/api/pods/{self.pod.id}/invites/{created['id']}/revoke"
        )
        self.assertEqual(response.status_code, 200)

        preview = flask_app.test_client().get(f"/api/invite/{created['token']}")
        self.assertEqual(preview.status_code, 410)

    def test_previewing_an_invite_needs_no_session(self):
        created = self.client.post(f"/api/pods/{self.pod.id}/invites", json={}).get_json()
        preview = flask_app.test_client().get(f"/api/invite/{created['token']}")
        self.assertEqual(preview.status_code, 200)
        body = preview.get_json()
        self.assertEqual(body["pod"]["name"], "Friday Crew")
        self.assertEqual(body["role"], "member")
        self.assertNotIn("token", body)

    def test_an_unknown_invite_token_is_indistinguishable_from_a_revoked_one(self):
        created = self.client.post(f"/api/pods/{self.pod.id}/invites", json={}).get_json()
        self.client.post(f"/api/pods/{self.pod.id}/invites/{created['id']}/revoke")
        anon = flask_app.test_client()
        revoked = anon.get(f"/api/invite/{created['token']}")
        unknown = anon.get("/api/invite/never-issued")
        self.assertEqual(revoked.status_code, unknown.status_code)
        self.assertEqual(revoked.get_json(), unknown.get_json())

    def test_accepting_an_invite_joins_the_pod(self):
        created = self.client.post(f"/api/pods/{self.pod.id}/invites", json={}).get_json()
        joiner = self._signed_in(self.outsider)
        response = joiner.post(f"/api/invite/{created['token']}")
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        self.assertEqual(response.get_json()["pod"]["id"], self.pod.id)

        membership = PodMembership.query.filter_by(
            pod_id=self.pod.id, player_id=self.outsider.player.id
        ).one()
        self.assertEqual(membership.role, "member")

    def test_accepting_spends_one_use_and_the_second_attempt_fails(self):
        created = self.client.post(
            f"/api/pods/{self.pod.id}/invites", json={"usage_limit": 1}
        ).get_json()
        self._signed_in(self.outsider).post(f"/api/invite/{created['token']}")

        third = self._make_user("third")
        response = self._signed_in(third).post(f"/api/invite/{created['token']}")
        self.assertEqual(response.status_code, 410)

    def test_accepting_twice_is_idempotent_for_the_same_person(self):
        created = self.client.post(
            f"/api/pods/{self.pod.id}/invites", json={"usage_limit": 5}
        ).get_json()
        joiner = self._signed_in(self.outsider)
        joiner.post(f"/api/invite/{created['token']}")
        again = joiner.post(f"/api/invite/{created['token']}")

        self.assertEqual(again.status_code, 200)
        self.assertEqual(
            PodMembership.query.filter_by(
                pod_id=self.pod.id, player_id=self.outsider.player.id
            ).count(), 1,
        )
        self.assertEqual(
            PodInvite.query.one().use_count, 1,
            "re-accepting must not burn another use",
        )

    def test_accepting_an_expired_invite_fails_closed(self):
        created = self.client.post(f"/api/pods/{self.pod.id}/invites", json={}).get_json()
        invite = PodInvite.query.one()
        invite.expires_at = datetime.utcnow() - timedelta(seconds=1)
        db.session.commit()
        response = self._signed_in(self.outsider).post(f"/api/invite/{created['token']}")
        self.assertEqual(response.status_code, 410)

    def test_accepting_requires_a_session(self):
        created = self.client.post(f"/api/pods/{self.pod.id}/invites", json={}).get_json()
        response = flask_app.test_client().post(f"/api/invite/{created['token']}")
        self.assertEqual(response.status_code, 401)

    # ----- P4-2 guest players ---------------------------------------------

    def test_adding_a_guest_creates_a_player_with_no_account(self):
        response = self.client.post(
            f"/api/pods/{self.pod.id}/guests", json={"name": "Visiting Dave"}
        )
        self.assertEqual(response.status_code, 201, response.get_data(as_text=True))
        body = response.get_json()
        self.assertEqual(body["name"], "Visiting Dave")
        self.assertTrue(body["is_guest"])

        player = Player.query.filter_by(name="Visiting Dave").one()
        self.assertIsNone(player.user_id, "a guest must not be attached to an account")
        PodMembership.query.filter_by(pod_id=self.pod.id, player_id=player.id).one()

    def test_a_guest_is_scoped_to_the_pod_that_added_them(self):
        # A record for someone who never signed up must not spread. The second pod is
        # owned by the *same* manager on purpose: a leak into pods they cannot reach
        # would be caught by the permission check anyway, but a leak into their own
        # other pods would not.
        my_other_pod = self._make_pod("My Other Pod", self.owner)
        strangers_pod = self._make_pod("Strangers Pod", self.outsider)

        self.client.post(f"/api/pods/{self.pod.id}/guests", json={"name": "Visiting Dave"})
        guest = Player.query.filter_by(name="Visiting Dave").one()

        self.assertEqual(
            PodMembership.query.filter_by(player_id=guest.id).count(), 1,
            "a guest belongs to exactly one pod",
        )
        for pod in (my_other_pod, strangers_pod):
            self.assertEqual(
                PodMembership.query.filter_by(pod_id=pod.id, player_id=guest.id).count(),
                0, f"guest leaked into {pod.name}",
            )

    def test_a_guest_needs_a_name(self):
        for name in ("", "   ", None):
            with self.subTest(name=name):
                response = self.client.post(
                    f"/api/pods/{self.pod.id}/guests", json={"name": name}
                )
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.get_json()["field"], "name")

    def test_an_overlong_guest_name_is_refused(self):
        response = self.client.post(
            f"/api/pods/{self.pod.id}/guests", json={"name": "g" * 5_000}
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["field"], "name")

    def test_only_a_pod_manager_may_add_a_guest(self):
        response = self._signed_in(self.outsider).post(
            f"/api/pods/{self.pod.id}/guests", json={"name": "Sneaky"}
        )
        self.assertEqual(response.status_code, 403)
        self.assertIsNone(Player.query.filter_by(name="Sneaky").first())

    # ----- P4-3 recap sharing ---------------------------------------------

    def test_publishing_a_recap_returns_a_public_link(self):
        game = self._finished_game()
        response = self.client.post(f"/api/games/{game.id}/shares", json={})
        self.assertEqual(response.status_code, 201, response.get_data(as_text=True))
        body = response.get_json()
        self.assertIn("/r/", body["share_url"])
        share = GameShare.query.one()
        self.assertEqual(share.token_hash, app_module.hash_public_token(body["token"]))

    def test_a_recap_hides_names_when_asked(self):
        game = self._finished_game()
        created = self.client.post(
            f"/api/games/{game.id}/shares",
            json={"show_player_names": False, "show_deck_names": False},
        ).get_json()

        recap = flask_app.test_client().get(f"/api/recap/{created['token']}")
        self.assertEqual(recap.status_code, 200)
        seat = recap.get_json()["participants"][0]
        self.assertNotIn("Owner", seat["player"])
        self.assertNotIn("Atraxa", seat["deck"])

    def test_an_unconfigured_recap_publishes_the_least_it_can(self):
        # The important default. A client that sends no switches must not be treated
        # as having consented to publishing everyone's names.
        game = self._finished_game()
        created = self.client.post(f"/api/games/{game.id}/shares", json={}).get_json()
        self.assertFalse(created["show_player_names"])
        self.assertFalse(created["show_deck_names"])

        seat = flask_app.test_client().get(
            f"/api/recap/{created['token']}"
        ).get_json()["participants"][0]
        self.assertEqual(seat["player"], "Player 1")
        self.assertEqual(seat["deck"], "Private deck")
        self.assertIsNone(seat["commander"])

    def test_a_recap_shows_names_by_default_when_requested(self):
        game = self._finished_game()
        created = self.client.post(
            f"/api/games/{game.id}/shares",
            json={"show_player_names": True, "show_deck_names": True},
        ).get_json()
        seat = flask_app.test_client().get(
            f"/api/recap/{created['token']}"
        ).get_json()["participants"][0]
        self.assertEqual(seat["player"], "Owner")
        self.assertEqual(seat["deck"], "Atraxa Superfriends")

    def test_a_recap_is_readable_without_a_session(self):
        game = self._finished_game()
        created = self.client.post(f"/api/games/{game.id}/shares", json={}).get_json()
        response = flask_app.test_client().get(f"/api/recap/{created['token']}")
        self.assertEqual(response.status_code, 200)

    def test_revoking_a_recap_takes_it_offline(self):
        game = self._finished_game()
        created = self.client.post(f"/api/games/{game.id}/shares", json={}).get_json()
        response = self.client.post(f"/api/games/{game.id}/shares/{created['id']}/revoke")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            flask_app.test_client().get(f"/api/recap/{created['token']}").status_code, 404
        )

    def test_listing_shares_never_returns_a_token(self):
        game = self._finished_game()
        created = self.client.post(f"/api/games/{game.id}/shares", json={}).get_json()
        listing = self.client.get(f"/api/games/{game.id}/shares")
        self.assertEqual(listing.status_code, 200)
        self.assertNotIn(created["token"], listing.get_data(as_text=True))
        self.assertEqual(listing.get_json()["shares"][0]["id"], created["id"])

    def test_an_unverified_account_may_not_publish(self):
        game = self._finished_game()
        unverified = self._make_user("nomail", verified=False)
        app_module.ensure_membership(self.pod.id, unverified.player.id)
        db.session.commit()
        response = self._signed_in(unverified).post(f"/api/games/{game.id}/shares", json={})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.get_json()["reason"], "email_unverified")

    def test_a_stranger_cannot_publish_someone_elses_game(self):
        game = self._finished_game()
        response = self._signed_in(self.outsider).post(
            f"/api/games/{game.id}/shares", json={}
        )
        self.assertEqual(response.status_code, 404)
        self.assertEqual(GameShare.query.count(), 0)

    def test_an_unknown_recap_token_is_a_404(self):
        self.assertEqual(
            flask_app.test_client().get("/api/recap/never-issued").status_code, 404
        )


if __name__ == "__main__":
    unittest.main()
