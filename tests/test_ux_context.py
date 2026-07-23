"""Phase 1 tests: deterministic entity identity and persistent scope context."""
import os
import re
import unittest
from pathlib import Path
from werkzeug.security import generate_password_hash

import app as app_module
from app import app as flask_app, db, User


def _setup_admin(client):
    with flask_app.app_context():
        u = User(
            username="ux_admin",
            display_name="UX Admin",
            password_hash=generate_password_hash("pass"),
            is_active=True,
            is_admin=True,
        )
        db.session.add(u)
        db.session.commit()
        uid = u.id
    with client.session_transaction() as sess:
        sess["user_id"] = uid
        sess["username"] = "ux_admin"
        sess["display_name"] = "UX Admin"
        sess["is_admin"] = True


class PlayerAccentTests(unittest.TestCase):
    """player_id_to_accent() must be deterministic and cycle the full palette."""

    def test_helper_exists(self):
        self.assertTrue(callable(getattr(app_module, "player_id_to_accent", None)))

    def test_palette_constant_exists(self):
        self.assertTrue(hasattr(app_module, "PLAYER_ACCENT_PALETTE"))
        palette = app_module.PLAYER_ACCENT_PALETTE
        self.assertIsInstance(palette, (list, tuple))
        self.assertGreaterEqual(len(palette), 4)

    def test_accent_is_deterministic(self):
        fn = app_module.player_id_to_accent
        for pid in range(1, 20):
            self.assertEqual(fn(pid), fn(pid))

    def test_accent_cycles_all_palette_entries(self):
        fn = app_module.player_id_to_accent
        palette = app_module.PLAYER_ACCENT_PALETTE
        seen = {fn(i) for i in range(len(palette))}
        self.assertEqual(len(seen), len(palette))

    def test_accent_returns_css_variable(self):
        fn = app_module.player_id_to_accent
        for pid in range(0, 18):
            result = fn(pid)
            self.assertTrue(
                result.startswith("var(--"),
                f"player_id {pid}: {result!r} must be a CSS var() reference",
            )


class ScopeSessionTests(unittest.TestCase):
    """game_query_for_scope() must persist validated scope to session."""

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

    def test_scope_all_persists_to_session(self):
        with flask_app.test_client() as client:
            _setup_admin(client)
            client.get("/?scope=all")
            with client.session_transaction() as sess:
                self.assertEqual(sess.get("nav_scope"), "all")

    def test_scope_pod_persists_to_session(self):
        with flask_app.test_client() as client:
            _setup_admin(client)
            client.get("/?scope=pod")
            with client.session_transaction() as sess:
                self.assertEqual(sess.get("nav_scope"), "pod")

    def test_invalid_scope_treated_as_pod(self):
        with flask_app.test_client() as client:
            _setup_admin(client)
            client.get("/?scope=evil")
            with client.session_transaction() as sess:
                self.assertEqual(sess.get("nav_scope", "pod"), "pod")

    def test_scope_defaults_to_pod_without_param(self):
        with flask_app.test_client() as client:
            _setup_admin(client)
            client.get("/")
            with client.session_transaction() as sess:
                self.assertIn(sess.get("nav_scope", "pod"), ("pod", None))


class NavScopeContextTests(unittest.TestCase):
    """inject_pod_context() must expose nav_scope in the template context."""

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

    def test_authenticated_home_page_contains_nav_scope(self):
        """The rendered homepage must expose the active scope via data-nav-scope."""
        with flask_app.test_client() as client:
            _setup_admin(client)
            resp = client.get("/")
            self.assertEqual(resp.status_code, 200)
            # The template renders data-nav-scope on the body (always-present).
            html = resp.data.decode()
            self.assertIn("data-nav-scope=", html,
                          msg="data-nav-scope not found in homepage HTML")


class ServiceWorkerAssetStrategyTests(unittest.TestCase):
    """First-party mutable CSS must not be permanently cache-first."""

    def _read_sw(self):
        return Path("static/sw.js").read_text(encoding="utf-8")

    def test_navigation_is_network_first(self):
        sw = self._read_sw()
        self.assertIn('event.request.mode === "navigate"', sw)
        self.assertIn("fetch(event.request).catch(() => caches.match(event.request))", sw)

    def test_home_page_not_in_static_assets(self):
        sw = self._read_sw()
        m = re.search(r"const STATIC_ASSETS = \[(.*?)\];", sw, re.S)
        self.assertIsNotNone(m, "STATIC_ASSETS not found in sw.js")
        self.assertNotIn('"/"', m.group(1))

    def test_first_party_css_not_permanently_cached(self):
        """base.css should NOT be in the permanent STATIC_ASSETS cache-first list."""
        sw = self._read_sw()
        m = re.search(r"const STATIC_ASSETS = \[(.*?)\];", sw, re.S)
        self.assertIsNotNone(m)
        static_assets = m.group(1)
        self.assertNotIn("/static/css/base.css", static_assets,
                         "base.css must not be permanently cache-first")

    def test_first_party_css_is_handled_by_sw(self):
        """sw.js must have a stale-while-revalidate or network-first path for /static/css/."""
        sw = self._read_sw()
        self.assertIn("/static/css/", sw,
                      "sw.js has no strategy for /static/css/ — first-party CSS will be unhandled")


if __name__ == "__main__":
    unittest.main()
