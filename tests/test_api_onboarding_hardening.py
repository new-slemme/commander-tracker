"""TASK-R27/R28/R29: the onboarding endpoints under hostile or degraded conditions.

Three separate concerns, kept together because they all guard the same entry point:

  * R27 -- a mail relay that is down must not turn a committed account into a 500.
    The caller has to learn that the account exists and that no email went out,
    or it retries and is told its own username is taken.
  * R28 -- the registration text fields need length bounds. SQLite does not enforce
    db.String(100), so without them a client can store megabytes per field.
  * R29 -- life-history samples must be length-checked before the per-element loop,
    so a huge list is refused rather than fully validated and then thrown away.
"""
import unittest
from datetime import datetime

from werkzeug.security import generate_password_hash

import app as app_module
from app import app as flask_app, db, User, Player, Pod, PodMembership, ActiveGame

VALID_PASSWORD = "StrongPass123"


def _registration_payload(**overrides):
    payload = {
        "username": "hardened",
        "display_name": "Hardened",
        "email": "hardened@example.test",
        "pod_name": "Friday Crew",
        "password": VALID_PASSWORD,
        "confirm": VALID_PASSWORD,
    }
    payload.update(overrides)
    return payload


class OnboardingHardeningTests(unittest.TestCase):

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
        db.session.query(PodMembership).delete()
        db.session.query(Pod).delete()
        db.session.query(Player).delete()
        db.session.query(User).delete()
        db.session.commit()
        self._real_send = app_module.send_transactional_email
        self.client = flask_app.test_client()

    def tearDown(self):
        app_module.send_transactional_email = self._real_send
        db.session.remove()
        self.ctx.pop()

    def _break_the_mail_relay(self):
        def explode(recipient, subject, body):
            raise OSError("[Errno 111] Connection refused")
        app_module.send_transactional_email = explode

    # ----- R27: a dead mail relay -----------------------------------------

    def test_registration_survives_a_dead_mail_relay(self):
        self._break_the_mail_relay()
        response = self.client.post("/api/register", json=_registration_payload())
        self.assertEqual(response.status_code, 201, response.get_data(as_text=True))
        body = response.get_json()
        self.assertFalse(
            body["verification_email_sent"],
            "the client must be told no email went out, so it can offer Resend",
        )
        self.assertFalse(body["email_verified"])

    def test_a_dead_mail_relay_still_signs_the_client_in(self):
        # Otherwise the account exists, the caller is anonymous, and its retry is
        # answered with 409 "username already taken" -- by its own account.
        self._break_the_mail_relay()
        self.client.post("/api/register", json=_registration_payload())
        me = self.client.get("/api/me")
        self.assertEqual(me.status_code, 200)
        self.assertEqual(me.get_json()["username"], "hardened")

    def test_the_account_is_intact_after_a_mail_failure(self):
        self._break_the_mail_relay()
        self.client.post("/api/register", json=_registration_payload())
        user = User.query.filter_by(username="hardened").one()
        self.assertIsNotNone(user.player)
        pod = Pod.query.filter_by(owner_user_id=user.id).one()
        membership = PodMembership.query.filter_by(
            pod_id=pod.id, player_id=user.player.id
        ).one()
        self.assertEqual(membership.role, "podmaster")
        self.assertIsNotNone(
            user.email_verification_token_hash,
            "the token must still be on file so a resend can mail it",
        )

    def test_a_healthy_relay_reports_the_email_as_sent(self):
        sent = []
        app_module.send_transactional_email = lambda *a: sent.append(a) or True
        response = self.client.post("/api/register", json=_registration_payload())
        self.assertTrue(response.get_json()["verification_email_sent"])
        self.assertEqual(len(sent), 1)

    def test_resend_survives_a_dead_mail_relay(self):
        app_module.send_transactional_email = lambda *a: True
        self.client.post("/api/register", json=_registration_payload())
        self._break_the_mail_relay()
        response = self.client.post("/api/email/verify/resend")
        self.assertEqual(response.status_code, 202, response.get_data(as_text=True))
        self.assertFalse(response.get_json()["verification_email_sent"])

    def test_forgot_password_survives_a_dead_mail_relay(self):
        user = User(
            username="mailless",
            display_name="Mailless",
            email="mailless@example.test",
            password_hash=generate_password_hash(VALID_PASSWORD),
            is_active=True,
            approved_at=datetime.utcnow(),
        )
        db.session.add(user)
        db.session.commit()
        self._break_the_mail_relay()

        response = self.client.post(
            "/api/password/forgot", json={"email": "mailless@example.test"}
        )
        # Still 202, still the same body: whether the mail left is not something an
        # anonymous caller gets to learn either.
        self.assertEqual(response.status_code, 202)
        self.assertEqual(
            response.get_json()["message"], app_module.PASSWORD_RESET_SENT_MESSAGE
        )
        db.session.refresh(user)
        self.assertIsNotNone(user.password_reset_token_hash)

    def test_the_web_form_survives_a_dead_mail_relay(self):
        self._break_the_mail_relay()
        response = self.client.post(
            "/register",
            data={
                "username": "webform",
                "display_name": "Web Form",
                "email": "webform@example.test",
                "pod_name": "Web Pod",
                "password": VALID_PASSWORD,
                "confirm": VALID_PASSWORD,
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertIsNotNone(User.query.filter_by(username="webform").first())

    # ----- R28: length bounds ---------------------------------------------

    def test_overlong_text_fields_are_refused_per_field(self):
        for field in ("username", "display_name", "email", "pod_name"):
            with self.subTest(field=field):
                value = "a" * 5_000
                if field == "email":
                    value = "a" * 5_000 + "@example.test"
                response = self.client.post(
                    "/api/register", json=_registration_payload(**{field: value})
                )
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.get_json()["field"], field)
                self.assertEqual(User.query.count(), 0)

    def test_a_field_at_the_limit_is_accepted(self):
        app_module.send_transactional_email = lambda *a: True
        response = self.client.post(
            "/api/register",
            json=_registration_payload(pod_name="p" * app_module.MAX_POD_NAME_LENGTH),
        )
        self.assertEqual(response.status_code, 201, response.get_data(as_text=True))

    def test_an_absurd_password_is_refused_rather_than_hashed(self):
        # generate_password_hash on a multi-megabyte string is real CPU, on an
        # unauthenticated endpoint.
        response = self.client.post(
            "/api/register",
            json=_registration_payload(password="a1" * 100_000, confirm="a1" * 100_000),
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["field"], "password")

    # ----- R29: life history length ---------------------------------------

    def test_an_absurd_life_history_is_refused_before_it_is_validated(self):
        samples, error = app_module._normalize_life_history(
            [[0, 40]] * (app_module.MAX_LIFE_HISTORY_SAMPLES * 100)
        )
        self.assertIsNone(samples)
        self.assertIsNotNone(error)

    def test_a_long_but_plausible_history_still_truncates_to_the_newest(self):
        raw = [[i, 40 - i] for i in range(app_module.MAX_LIFE_HISTORY_SAMPLES + 30)]
        samples, error = app_module._normalize_life_history(raw)
        self.assertIsNone(error)
        self.assertEqual(len(samples), app_module.MAX_LIFE_HISTORY_SAMPLES)
        self.assertEqual(samples[-1], raw[-1], "the newest sample is kept")

    def test_a_normal_history_is_unaffected(self):
        samples, error = app_module._normalize_life_history([[1, 40], [2, 37], [3, 30]])
        self.assertIsNone(error)
        self.assertEqual(samples, [[1, 40], [2, 37], [3, 30]])


if __name__ == "__main__":
    unittest.main()
