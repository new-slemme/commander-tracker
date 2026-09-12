"""GET /api/capabilities — lets a client discover what this server supports.

The Android client ships ahead of, and behind, the server. Without a capability
probe it has to guess: call an endpoint and treat 404 as "not supported", which is
indistinguishable from a typo'd path or a misconfigured base URL.

The payload is split by audience. Anonymous callers get only the flags that gate
pre-auth UI -- whether to offer "Create account" and "Forgot password" -- and those
describe web routes (/register, /forgot-password, /verify-email/<token>) that are
already publicly discoverable, so the public half reveals nothing new. The rest is
only actionable after signing in, and an unshipped feature list is not something to
hand to anonymous callers, so it sits behind the session.
"""
import json
import unittest

from werkzeug.security import generate_password_hash

from app import app as flask_app, db, User, Player


# Gate pre-auth UI, and mirror publicly reachable web routes.
PUBLIC_FEATURE_KEYS = {
    "registration",
    "password_reset",
    "email_verification",
}

# Only meaningful once signed in.
AUTHENTICATED_ONLY_FEATURE_KEYS = {
    "search",
    "compare",
    "mmr",
    "life_history",
    "pod_invites",
    "guest_players",
    "game_shares",
    "pod_config",
    "account_export",
}

ALL_FEATURE_KEYS = PUBLIC_FEATURE_KEYS | AUTHENTICATED_ONLY_FEATURE_KEYS


class CapabilitiesEndpointTests(unittest.TestCase):

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

    def tearDown(self):
        db.session.remove()
        self.ctx.pop()

    def _anonymous(self):
        resp = flask_app.test_client().get("/api/capabilities")
        return resp, json.loads(resp.data)

    def _authenticated(self):
        user = User(
            username="cap_user",
            display_name="Cap User",
            password_hash=generate_password_hash("pass"),
            is_active=True,
        )
        db.session.add(user)
        db.session.flush()
        player = Player(name=user.display_name, user_id=user.id)
        db.session.add(player)
        db.session.flush()
        user.player = player
        db.session.commit()

        client = flask_app.test_client()
        with client.session_transaction() as sess:
            sess["user_id"] = user.id
            sess["session_version"] = user.session_version
        resp = client.get("/api/capabilities")
        return resp, json.loads(resp.data)

    # ---- shape ----

    def test_reachable_without_a_session(self):
        resp, _ = self._anonymous()
        self.assertEqual(
            resp.status_code, 200,
            "must stay public so a client can decide about a signup screen before login",
        )

    def test_reports_a_contract_version_to_both_audiences(self):
        for label, (_, body) in (("anonymous", self._anonymous()), ("signed in", self._authenticated())):
            self.assertIn("contract_version", body, label)
            self.assertIsInstance(body["contract_version"], int, label)
            self.assertGreaterEqual(body["contract_version"], 1, label)

    def test_features_is_a_flat_bool_map_for_both_audiences(self):
        for label, (_, body) in (("anonymous", self._anonymous()), ("signed in", self._authenticated())):
            features = body["features"]
            self.assertIsInstance(features, dict, label)
            non_bool = {k: v for k, v in features.items() if not isinstance(v, bool)}
            self.assertEqual(non_bool, {}, f"{label}: feature flags must be booleans, got {non_bool}")

    # ---- audience split ----

    def test_anonymous_sees_exactly_the_public_flags(self):
        _, body = self._anonymous()
        self.assertEqual(
            set(body["features"]), PUBLIC_FEATURE_KEYS,
            "anonymous callers get the flags that gate pre-auth UI and nothing else",
        )

    def test_anonymous_does_not_see_the_unshipped_roadmap(self):
        _, body = self._anonymous()
        leaked = sorted(AUTHENTICATED_ONLY_FEATURE_KEYS & set(body["features"]))
        self.assertEqual(
            leaked, [],
            f"these are not for anonymous callers: {leaked}",
        )

    def test_signed_in_sees_every_flag(self):
        _, body = self._authenticated()
        missing = sorted(ALL_FEATURE_KEYS - set(body["features"]))
        self.assertEqual(
            missing, [],
            f"a signed-in client branches on these keys; missing: {missing}",
        )

    # ---- honesty of the flags ----

    def test_shipped_features_report_true(self):
        """Guards against advertising a capability the server does not actually have."""
        _, body = self._authenticated()
        shipped = (
            "search", "compare", "mmr", "life_history",
            "registration", "password_reset", "email_verification",
        )
        for key in shipped:
            self.assertTrue(body["features"][key], f"'{key}' is implemented today")

    def test_unimplemented_features_report_false(self):
        """Flip these to true in the same commit that lands the endpoint."""
        _, body = self._authenticated()
        for key in ("pod_invites", "guest_players", "game_shares", "pod_config", "account_export"):
            self.assertFalse(
                body["features"][key],
                f"'{key}' has no JSON API yet — advertising it true would make "
                "clients call an endpoint that does not exist",
            )

    def test_public_flags_are_explicit_false_not_omitted(self):
        """An explicit false distinguishes 'off here' from 'server predates it'."""
        _, body = self._anonymous()
        for key in PUBLIC_FEATURE_KEYS:
            self.assertIn(key, body["features"])


if __name__ == "__main__":
    unittest.main()
