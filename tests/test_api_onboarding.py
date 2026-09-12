"""Phase 3: the Android client must be able to onboard without a browser.

Everything here is credential handling on unauthenticated endpoints, so the tests
are written around the failure modes that matter rather than the happy path alone:

  * account enumeration -- /api/password/forgot must answer identically for a
    registered and an unregistered address, and a bad reset token must not be
    distinguishable from an expired one.
  * token handling -- reset and verification tokens are single use, expire, and
    are never stored in a form that a database read would hand back.
  * session revocation -- completing a password reset must invalidate sessions
    that were already open, or a stolen session survives the remedy for the theft.

Tokens are recovered the way a user would get them: by reading the email that was
sent. That keeps the delivery path inside the test rather than reaching around it.
"""
import re
import unittest
from datetime import datetime, timedelta

from werkzeug.security import check_password_hash, generate_password_hash

import app as app_module
from app import app as flask_app, db, User, Player, Pod, PodMembership

VALID_PASSWORD = "StrongPass123"
ANOTHER_PASSWORD = "OtherPass456"
TOKEN_IN_URL_RE = re.compile(r"https?://\S+/(?:verify-email|reset-password)/(\S+)")


def _registration_payload(**overrides):
    payload = {
        "username": "newcomer",
        "display_name": "Newcomer",
        "email": "newcomer@example.test",
        "pod_name": "Friday Crew",
        "password": VALID_PASSWORD,
        "confirm": VALID_PASSWORD,
    }
    payload.update(overrides)
    return payload


class ApiOnboardingTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        flask_app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
        flask_app.config["TESTING"] = True
        # These endpoints are deliberately rate limited; exercising them more than
        # five times in a run would otherwise trip the limiter instead of the code
        # under test. One test re-enables it to prove the limit is really wired up.
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
        db.session.query(PodMembership).delete()
        db.session.query(Pod).delete()
        db.session.query(Player).delete()
        db.session.query(User).delete()
        db.session.commit()

        self.sent_emails = []
        self._real_send = app_module.send_transactional_email
        app_module.send_transactional_email = self._capture_email
        self.client = flask_app.test_client()

    def tearDown(self):
        app_module.send_transactional_email = self._real_send
        db.session.remove()
        self.ctx.pop()

    def _capture_email(self, recipient, subject, body):
        self.sent_emails.append({"to": recipient, "subject": subject, "body": body})
        return True

    def _token_from_last_email(self):
        self.assertTrue(self.sent_emails, "expected an email to have been sent")
        match = TOKEN_IN_URL_RE.search(self.sent_emails[-1]["body"])
        self.assertIsNotNone(match, f"no token URL in: {self.sent_emails[-1]['body']!r}")
        return match.group(1)

    def _register(self, **overrides):
        return self.client.post("/api/register", json=_registration_payload(**overrides))

    def _make_verified_user(self, username="existing", email="existing@example.test"):
        user = User(
            username=username,
            display_name=username.title(),
            email=email,
            password_hash=generate_password_hash(VALID_PASSWORD),
            is_active=True,
            approved_at=datetime.utcnow(),
            email_verified_at=datetime.utcnow(),
        )
        db.session.add(user)
        db.session.flush()
        user.player = Player(name=user.display_name, user_id=user.id)
        db.session.commit()
        return user

    # ----- registration -------------------------------------------------

    def test_register_creates_account_player_and_owned_pod(self):
        response = self._register()
        self.assertEqual(response.status_code, 201, response.get_data(as_text=True))
        body = response.get_json()

        user = User.query.filter_by(username="newcomer").one()
        self.assertEqual(body["user_id"], user.id)
        self.assertEqual(body["player_id"], user.player.id)
        self.assertFalse(body["email_verified"])

        pod = Pod.query.filter_by(owner_user_id=user.id).one()
        self.assertEqual(pod.name, "Friday Crew")
        self.assertEqual(body["pod"]["id"], pod.id)
        membership = PodMembership.query.filter_by(pod_id=pod.id, player_id=user.player.id).one()
        self.assertEqual(membership.role, "podmaster")

    def test_register_signs_the_client_in(self):
        # The web flow bounces to a login form. A phone has nowhere to bounce to,
        # so registration establishes the session it just earned.
        self._register()
        me = self.client.get("/api/me")
        self.assertEqual(me.status_code, 200)
        self.assertEqual(me.get_json()["username"], "newcomer")

    def test_register_sends_a_verification_email_and_stores_only_its_hash(self):
        self._register()
        raw_token = self._token_from_last_email()

        user = User.query.filter_by(username="newcomer").one()
        self.assertIsNone(user.email_verified_at)
        self.assertNotEqual(user.email_verification_token_hash, raw_token)
        self.assertEqual(
            user.email_verification_token_hash, app_module.hash_public_token(raw_token)
        )
        self.assertGreater(user.email_verification_expires_at, datetime.utcnow())

    def test_register_rejects_a_weak_password_without_creating_anything(self):
        response = self._register(password="short", confirm="short")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["field"], "password")
        self.assertEqual(User.query.count(), 0)
        self.assertEqual(Pod.query.count(), 0)

    def test_register_rejects_mismatched_confirmation(self):
        response = self._register(confirm=ANOTHER_PASSWORD)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["field"], "confirm")

    def test_register_rejects_a_malformed_email(self):
        response = self._register(email="not-an-email")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["field"], "email")

    def test_register_names_the_missing_field(self):
        # Text fields are trimmed, so whitespace reads as absent. A password is not
        # trimmed -- spaces are legitimate characters in one -- so it is sent empty.
        blank_by_field = {
            "username": "   ",
            "display_name": "   ",
            "email": "   ",
            "pod_name": "   ",
            "password": "",
        }
        for field, blank in blank_by_field.items():
            with self.subTest(field=field):
                response = self._register(**{field: blank})
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.get_json()["field"], field)

    def test_register_rejects_a_body_that_is_not_an_object(self):
        response = self.client.post("/api/register", json=["newcomer"])
        self.assertEqual(response.status_code, 400)

    def test_register_conflicts_are_reported_per_field(self):
        self._make_verified_user(username="taken", email="taken@example.test")

        by_username = self._register(username="taken")
        self.assertEqual(by_username.status_code, 409)
        self.assertEqual(by_username.get_json()["field"], "username")

        by_email = self._register(email="taken@example.test")
        self.assertEqual(by_email.status_code, 409)
        self.assertEqual(by_email.get_json()["field"], "email")

    def test_register_matches_an_existing_email_case_insensitively(self):
        self._make_verified_user(username="taken", email="taken@example.test")
        response = self._register(email="TAKEN@Example.Test")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.get_json()["field"], "email")

    # ----- forgotten password -------------------------------------------

    def test_forgot_password_answers_identically_for_known_and_unknown_addresses(self):
        self._make_verified_user()

        known = self.client.post("/api/password/forgot", json={"email": "existing@example.test"})
        unknown = self.client.post("/api/password/forgot", json={"email": "nobody@example.test"})

        self.assertEqual(known.status_code, unknown.status_code)
        self.assertEqual(known.get_json(), unknown.get_json())

    def test_forgot_password_issues_a_token_only_for_a_real_account(self):
        user = self._make_verified_user()

        self.client.post("/api/password/forgot", json={"email": "nobody@example.test"})
        self.assertEqual(self.sent_emails, [])
        db.session.refresh(user)
        self.assertIsNone(user.password_reset_token_hash)

        self.client.post("/api/password/forgot", json={"email": "existing@example.test"})
        self.assertEqual(len(self.sent_emails), 1)
        db.session.refresh(user)
        self.assertEqual(
            user.password_reset_token_hash,
            app_module.hash_public_token(self._token_from_last_email()),
        )

    def test_forgot_password_ignores_a_soft_deleted_account(self):
        user = self._make_verified_user()
        user.deleted_at = datetime.utcnow()
        db.session.commit()

        response = self.client.post(
            "/api/password/forgot", json={"email": "existing@example.test"}
        )
        self.assertEqual(response.status_code, 202)
        self.assertEqual(self.sent_emails, [])

    # ----- password reset -----------------------------------------------

    def test_reset_changes_the_password_and_consumes_the_token(self):
        user = self._make_verified_user()
        self.client.post("/api/password/forgot", json={"email": user.email})
        token = self._token_from_last_email()

        response = self.client.post(
            "/api/password/reset", json={"token": token, "password": ANOTHER_PASSWORD}
        )
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))

        db.session.refresh(user)
        self.assertTrue(check_password_hash(user.password_hash, ANOTHER_PASSWORD))
        self.assertIsNone(user.password_reset_token_hash)

        replay = self.client.post(
            "/api/password/reset", json={"token": token, "password": VALID_PASSWORD}
        )
        self.assertEqual(replay.status_code, 400)

    def test_reset_revokes_sessions_that_were_already_open(self):
        # The whole point of a reset is often that someone else has the account.
        # Leaving their existing session valid would defeat it.
        user = self._make_verified_user()
        before = user.session_version

        stolen = flask_app.test_client()
        stolen.post("/api/login", json={"username": user.username, "password": VALID_PASSWORD})
        self.assertEqual(stolen.get("/api/me").status_code, 200)

        self.client.post("/api/password/forgot", json={"email": user.email})
        token = self._token_from_last_email()
        self.client.post(
            "/api/password/reset", json={"token": token, "password": ANOTHER_PASSWORD}
        )

        db.session.refresh(user)
        self.assertEqual(user.session_version, before + 1)
        self.assertEqual(stolen.get("/api/me").status_code, 401)

    def test_reset_refuses_an_expired_token_and_leaves_the_password_alone(self):
        user = self._make_verified_user()
        self.client.post("/api/password/forgot", json={"email": user.email})
        token = self._token_from_last_email()
        user.password_reset_expires_at = datetime.utcnow() - timedelta(seconds=1)
        db.session.commit()

        response = self.client.post(
            "/api/password/reset", json={"token": token, "password": ANOTHER_PASSWORD}
        )
        self.assertEqual(response.status_code, 400)
        db.session.refresh(user)
        self.assertTrue(check_password_hash(user.password_hash, VALID_PASSWORD))

    def test_reset_does_not_reveal_whether_a_token_ever_existed(self):
        user = self._make_verified_user()
        self.client.post("/api/password/forgot", json={"email": user.email})
        token = self._token_from_last_email()
        user.password_reset_expires_at = datetime.utcnow() - timedelta(seconds=1)
        db.session.commit()

        expired = self.client.post(
            "/api/password/reset", json={"token": token, "password": ANOTHER_PASSWORD}
        )
        unknown = self.client.post(
            "/api/password/reset", json={"token": "never-issued", "password": ANOTHER_PASSWORD}
        )
        self.assertEqual(expired.status_code, unknown.status_code)
        self.assertEqual(expired.get_json(), unknown.get_json())

    def test_reset_enforces_the_password_rules(self):
        user = self._make_verified_user()
        self.client.post("/api/password/forgot", json={"email": user.email})
        token = self._token_from_last_email()

        response = self.client.post(
            "/api/password/reset", json={"token": token, "password": "short"}
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["field"], "password")
        db.session.refresh(user)
        self.assertIsNotNone(
            user.password_reset_token_hash, "a rejected attempt must not burn the token"
        )

    # ----- email verification -------------------------------------------

    def test_verification_marks_the_account_and_is_single_use(self):
        self._register()
        token = self._token_from_last_email()

        response = self.client.post("/api/email/verify", json={"token": token})
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        self.assertTrue(response.get_json()["email_verified"])

        user = User.query.filter_by(username="newcomer").one()
        self.assertIsNotNone(user.email_verified_at)
        self.assertIsNone(user.email_verification_token_hash)

        replay = self.client.post("/api/email/verify", json={"token": token})
        self.assertEqual(replay.status_code, 400)

    def test_verification_refuses_an_expired_token(self):
        self._register()
        token = self._token_from_last_email()
        user = User.query.filter_by(username="newcomer").one()
        user.email_verification_expires_at = datetime.utcnow() - timedelta(seconds=1)
        db.session.commit()

        response = self.client.post("/api/email/verify", json={"token": token})
        self.assertEqual(response.status_code, 400)
        db.session.refresh(user)
        self.assertIsNone(user.email_verified_at)

    def test_resend_requires_a_session(self):
        response = self.client.post("/api/email/verify/resend")
        self.assertEqual(response.status_code, 401)

    def test_resend_issues_a_new_token_and_retires_the_old_one(self):
        self._register()
        first_token = self._token_from_last_email()

        response = self.client.post("/api/email/verify/resend")
        self.assertEqual(response.status_code, 202, response.get_data(as_text=True))
        second_token = self._token_from_last_email()
        self.assertNotEqual(first_token, second_token)

        stale = self.client.post("/api/email/verify", json={"token": first_token})
        self.assertEqual(stale.status_code, 400)
        fresh = self.client.post("/api/email/verify", json={"token": second_token})
        self.assertEqual(fresh.status_code, 200)

    def test_resend_is_a_no_op_once_the_address_is_verified(self):
        self._register()
        token = self._token_from_last_email()
        self.client.post("/api/email/verify", json={"token": token})
        self.sent_emails.clear()

        response = self.client.post("/api/email/verify/resend")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["email_verified"])
        self.assertEqual(self.sent_emails, [])

    # ----- /api/me -------------------------------------------------------

    def test_me_carries_what_the_client_needs_to_render_account_state(self):
        self._register()
        body = self.client.get("/api/me").get_json()

        user = User.query.filter_by(username="newcomer").one()
        pod = Pod.query.filter_by(owner_user_id=user.id).one()
        self.assertEqual(body["email"], "newcomer@example.test")
        self.assertFalse(body["email_verified"])
        self.assertEqual(body["session_version"], user.session_version)
        self.assertEqual(body["owned_pod_ids"], [pod.id])
        self.assertFalse(body["use_sigtaara"])

    def test_me_reflects_verification_once_it_happens(self):
        self._register()
        self.client.post("/api/email/verify", json={"token": self._token_from_last_email()})
        self.assertTrue(self.client.get("/api/me").get_json()["email_verified"])

    # ----- transport contract --------------------------------------------

    def test_onboarding_endpoints_answer_json_never_a_redirect(self):
        cases = (
            ("/api/register", {"username": ""}),
            ("/api/password/forgot", {"email": "nobody@example.test"}),
            ("/api/password/reset", {"token": "nope", "password": VALID_PASSWORD}),
            ("/api/email/verify", {"token": "nope"}),
        )
        for path, payload in cases:
            with self.subTest(path=path):
                response = self.client.post(path, json=payload)
                self.assertLess(response.status_code, 500)
                self.assertNotIn(response.status_code, (301, 302, 303, 307, 308))
                self.assertEqual(response.mimetype, "application/json")

    def test_registration_is_rate_limited(self):
        app_module.limiter.enabled = True
        try:
            statuses = [
                self._register(username=f"flood{i}", email=f"flood{i}@example.test").status_code
                for i in range(7)
            ]
        finally:
            app_module.limiter.enabled = False
            app_module.limiter.reset()
        self.assertIn(429, statuses, f"expected the 5/hour limit to bite: {statuses}")


if __name__ == "__main__":
    unittest.main()
