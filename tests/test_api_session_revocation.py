"""A revoked session must fail an /api request as JSON 401, never as an HTML redirect.

require_login() has three ways to reject a request. The unauthenticated branch
correctly returns {"error": "Unauthorized"} with 401 for /api paths. The other two --
a deleted user, and a session_version mismatch after an admin identity edit or a
password reset -- fall through to redirect(url_for("login")).

For a browser that is right. For the Android client it is not: OkHttp follows the
redirect, receives 200 with the HTML login page, fails to deserialize it, and shows
the user "Server returned HTML instead of JSON. Check Server URL" -- pointing them at
their server configuration when the real cause is that their session was revoked.

These tests pin the API contract: /api never answers with a redirect.
"""
import json
import unittest

from werkzeug.security import generate_password_hash

from app import app as flask_app, db, User, Player


def _make_user(username="revoked_user"):
    u = User(
        username=username,
        display_name=username.replace("_", " ").title(),
        password_hash=generate_password_hash("pass"),
        is_active=True,
    )
    db.session.add(u)
    db.session.flush()
    p = Player(name=u.display_name, user_id=u.id)
    db.session.add(p)
    db.session.flush()
    u.player = p
    db.session.commit()
    return u


class ApiSessionRevocationTests(unittest.TestCase):

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
        self.ctx = flask_app.app_context()
        self.ctx.push()
        db.session.query(Player).delete()
        db.session.query(User).delete()
        db.session.commit()
        self.user = _make_user()
        self.client = flask_app.test_client()

    def tearDown(self):
        db.session.remove()
        self.ctx.pop()

    def _sign_in(self, session_version=None):
        with self.client.session_transaction() as sess:
            sess["user_id"] = self.user.id
            sess["session_version"] = (
                self.user.session_version if session_version is None else session_version
            )

    def _assert_json_401(self, resp, context):
        self.assertEqual(
            resp.status_code, 401,
            f"{context}: expected 401, got {resp.status_code} "
            f"(Location: {resp.headers.get('Location')!r}). A redirect here reaches "
            f"the client as an HTML login page and is misreported as a bad server URL.",
        )
        self.assertEqual(
            resp.headers.get("Content-Type", "").split(";")[0], "application/json",
            f"{context}: response must be JSON, got {resp.headers.get('Content-Type')!r}",
        )
        self.assertIn("error", json.loads(resp.data))

    def test_baseline_unauthenticated_api_returns_json_401(self):
        """The branch that already behaves correctly — guards against regressing it."""
        self._assert_json_401(self.client.get("/api/players"), "unauthenticated")

    def test_session_version_mismatch_returns_json_401(self):
        self._sign_in(session_version=self.user.session_version + 1)
        self._assert_json_401(self.client.get("/api/players"), "revoked session_version")

    def test_deleted_user_returns_json_401(self):
        from datetime import datetime
        self._sign_in()
        self.user.deleted_at = datetime.utcnow()
        db.session.commit()
        self._assert_json_401(self.client.get("/api/players"), "deleted user")

    def test_missing_user_returns_json_401(self):
        with self.client.session_transaction() as sess:
            sess["user_id"] = 999999
            sess["session_version"] = 1
        self._assert_json_401(self.client.get("/api/players"), "user row gone")

    def test_revoked_session_is_cleared(self):
        """A revoked session must not survive to be retried."""
        self._sign_in(session_version=self.user.session_version + 1)
        self.client.get("/api/players")
        with self.client.session_transaction() as sess:
            self.assertNotIn(
                "user_id", sess,
                "the revoked session must be cleared, not left for the next request",
            )

    def test_web_routes_still_redirect(self):
        """The browser flow must keep its redirect — this fix is scoped to /api."""
        self._sign_in(session_version=self.user.session_version + 1)
        resp = self.client.get("/players")
        self.assertIn(
            resp.status_code, (301, 302),
            f"web routes must still redirect to login, got {resp.status_code}",
        )


if __name__ == "__main__":
    unittest.main()
