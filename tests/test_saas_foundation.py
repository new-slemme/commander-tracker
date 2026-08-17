import re
import unittest
from datetime import datetime, timedelta

from werkzeug.security import generate_password_hash

import app


class SaasFoundationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        app.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)

    def setUp(self):
        with app.app.app_context():
            app.db.session.remove()
            app.db.drop_all()
            app.db.create_all()

    def _user_and_pod(self, suffix, *, display_name="Alex", verified=True):
        user = app.User(
            username=f"user-{suffix}",
            display_name=display_name,
            email=f"{suffix}@example.test",
            email_verified_at=datetime.utcnow() if verified else None,
            password_hash=generate_password_hash("StrongPass123!"),
            is_active=True,
        )
        app.db.session.add(user)
        app.db.session.flush()
        player = app.Player(name=display_name, user_id=user.id)
        app.db.session.add(player)
        app.db.session.flush()
        pod = app.Pod(
            name=f"Friday Pod {suffix}",
            slug=f"friday-pod-{suffix}",
            owner_user_id=user.id,
        )
        app.db.session.add(pod)
        app.db.session.flush()
        app.db.session.add(
            app.PodMembership(pod_id=pod.id, player_id=player.id, role="podmaster")
        )
        return user, player, pod

    def _login(self, client, user, pod=None):
        with client.session_transaction() as session:
            session["user_id"] = user.id
            session["session_version"] = user.session_version
            if pod:
                session["active_pod_id"] = pod.id

    def test_public_landing_pricing_and_privacy_are_reachable(self):
        client = app.app.test_client()
        self.assertIn("Remember the games", client.get("/").get_data(as_text=True))
        self.assertEqual(client.get("/pricing").status_code, 200)
        self.assertEqual(client.get("/privacy").status_code, 200)

    def test_signup_creates_owned_pod_and_allows_same_display_name_in_another_pod(self):
        client = app.app.test_client()
        for suffix in ("one", "two"):
            response = client.post(
                "/register",
                data={
                    "username": f"signup-{suffix}",
                    "email": f"signup-{suffix}@example.test",
                    "display_name": "Alex",
                    "pod_name": "Friday Crew",
                    "password": "StrongPass123!",
                    "confirm": "StrongPass123!",
                },
            )
            self.assertEqual(response.status_code, 302)

        with app.app.app_context():
            users = app.User.query.filter(app.User.username.like("signup-%")).all()
            self.assertEqual(len(users), 2)
            self.assertEqual(app.Player.query.filter_by(name="Alex").count(), 2)
            for user in users:
                pod = app.Pod.query.filter_by(owner_user_id=user.id).one()
                membership = app.PodMembership.query.filter_by(
                    pod_id=pod.id, player_id=user.player.id
                ).one()
                self.assertEqual(membership.role, "podmaster")

    def test_invite_is_hashed_single_use_and_expiry_and_revocation_fail_closed(self):
        with app.app.app_context():
            owner, _, pod = self._user_and_pod("invite-owner")
            app.db.session.commit()
            owner_id, pod_id = owner.id, pod.id

        owner_client = app.app.test_client()
        with app.app.app_context():
            owner = app.db.session.get(app.User, owner_id)
            pod = app.db.session.get(app.Pod, pod_id)
            self._login(owner_client, owner, pod)
        response = owner_client.post(
            f"/pods/{pod_id}/invites",
            data={"expires_days": "7", "usage_limit": "1", "role": "member"},
        )
        self.assertEqual(response.status_code, 200)
        match = re.search(r'value="([^"]+/invite/([^"/]+))"', response.get_data(as_text=True))
        self.assertIsNotNone(match)
        raw_token = match.group(2)

        with app.app.app_context():
            invite = app.PodInvite.query.one()
            self.assertNotEqual(invite.token_hash, raw_token)
            self.assertEqual(invite.token_hash, app.hash_public_token(raw_token))

        guest = app.app.test_client()
        accepted = guest.post(
            f"/invite/{raw_token}",
            data={
                "username": "invited-user",
                "display_name": "Alex",
                "email": "invited@example.test",
                "password": "StrongPass123!",
                "confirm": "StrongPass123!",
            },
        )
        self.assertEqual(accepted.status_code, 302)
        self.assertEqual(app.app.test_client().get(f"/invite/{raw_token}").status_code, 410)

        with app.app.app_context():
            expired_raw, expired_hash = app.issue_public_token()
            revoked_raw, revoked_hash = app.issue_public_token()
            app.db.session.add_all([
                app.PodInvite(token_hash=expired_hash, pod_id=pod_id, role="member", expires_at=datetime.utcnow() - timedelta(seconds=1), usage_limit=1, created_by_user_id=owner_id),
                app.PodInvite(token_hash=revoked_hash, pod_id=pod_id, role="member", expires_at=datetime.utcnow() + timedelta(days=1), usage_limit=1, created_by_user_id=owner_id, revoked_at=datetime.utcnow()),
            ])
            app.db.session.commit()
        self.assertEqual(app.app.test_client().get(f"/invite/{expired_raw}").status_code, 410)
        self.assertEqual(app.app.test_client().get(f"/invite/{revoked_raw}").status_code, 410)

    def test_cross_pod_id_enumeration_and_search_are_isolated(self):
        with app.app.app_context():
            user_a, player_a, pod_a = self._user_and_pod("a")
            user_b, player_b, pod_b = self._user_and_pod("b")
            deck_a = app.Deck(name="Same Deck", commander="Admiral Brass", player_id=player_a.id, decklist_text="1 Island")
            deck_b = app.Deck(name="Same Deck", commander="Secret Commander", player_id=player_b.id, decklist_text="1 Swamp")
            app.db.session.add_all([deck_a, deck_b])
            app.db.session.flush()
            game_b = app.Game(winner_id=player_b.id, pod_id=pod_b.id, note="private note")
            app.db.session.add(game_b)
            app.db.session.flush()
            app.db.session.add(app.GameParticipant(game_id=game_b.id, player_id=player_b.id, deck_id=deck_b.id, seat_position=1))
            app.db.session.commit()
            ids = (user_a.id, pod_a.id, player_b.id, deck_b.id, game_b.id)

        client = app.app.test_client()
        with app.app.app_context():
            user_a = app.db.session.get(app.User, ids[0])
            pod_a = app.db.session.get(app.Pod, ids[1])
            self._login(client, user_a, pod_a)
        self.assertEqual(client.get(f"/player/{ids[2]}").status_code, 404)
        self.assertEqual(client.get(f"/deck/{ids[3]}").status_code, 404)
        self.assertEqual(client.get(f"/games/{ids[4]}").status_code, 404)
        self.assertEqual(client.get(f"/api/games/{ids[4]}").status_code, 404)
        search = client.get("/api/search?q=Secret").get_json()
        self.assertEqual(search["decks"], [])

    def test_public_recap_uses_allowlist_and_revocation(self):
        with app.app.app_context():
            owner, player, pod = self._user_and_pod("recap", display_name="Visible Player")
            deck = app.Deck(name="Private Deck Name", commander="Commander", player_id=player.id, decklist_text="1 Secret Card")
            app.db.session.add(deck)
            app.db.session.flush()
            game = app.Game(winner_id=player.id, pod_id=pod.id, note="private game note", ending_turn=8)
            app.db.session.add(game)
            app.db.session.flush()
            app.db.session.add(app.GameParticipant(game_id=game.id, player_id=player.id, deck_id=deck.id, seat_position=1, mmr_delta=10))
            app.db.session.commit()
            ids = owner.id, pod.id, game.id

        client = app.app.test_client()
        with app.app.app_context():
            self._login(client, app.db.session.get(app.User, ids[0]), app.db.session.get(app.Pod, ids[1]))
        created = client.post(
            f"/games/{ids[2]}/share",
            data={"show_player_names": "0", "show_deck_names": "0"},
        )
        self.assertEqual(created.status_code, 200)
        match = re.search(r'href="([^"]+/r/([^"/]+))"', created.get_data(as_text=True))
        self.assertIsNotNone(match)
        raw_token = match.group(2)
        public_html = app.app.test_client().get(f"/r/{raw_token}").get_data(as_text=True)
        for private_value in ("Visible Player", "Private Deck Name", "Secret Card", "private game note", "user-recap"):
            self.assertNotIn(private_value, public_html)
        self.assertIn("Player 1", public_html)
        self.assertIn("Private deck", public_html)

        with app.app.app_context():
            share = app.GameShare.query.one()
            share_id = share.id
        self.assertEqual(client.post(f"/games/{ids[2]}/shares/{share_id}/revoke").status_code, 302)
        self.assertEqual(app.app.test_client().get(f"/r/{raw_token}").status_code, 404)


if __name__ == "__main__":
    unittest.main()
