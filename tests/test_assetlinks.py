"""Digital Asset Links: without this, the Android App Link does not work.

The emailed verification and reset links are ordinary https URLs. Android only
hands them to the app instead of the browser if this file is reachable, unauthenticated,
and names the app's package and release signing certificate. Getting it wrong fails
silently -- the link just opens a web page -- so it is pinned here.
"""
import json
import unittest

from app import app as flask_app, db


class AssetLinksTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        flask_app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
        flask_app.config["TESTING"] = True
        with flask_app.app_context():
            db.create_all()

    def setUp(self):
        self.client = flask_app.test_client()

    def test_it_is_reachable_without_a_session(self):
        response = self.client.get("/.well-known/assetlinks.json")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "application/json")

    def test_it_grants_the_app_permission_to_handle_our_links(self):
        statements = self.client.get("/.well-known/assetlinks.json").get_json()
        self.assertIsInstance(statements, list)
        self.assertTrue(statements)
        statement = statements[0]
        self.assertIn("delegate_permission/common.handle_all_urls", statement["relation"])
        target = statement["target"]
        self.assertEqual(target["namespace"], "android_app")
        self.assertEqual(target["package_name"], "de.slemme.edhcompanion")

    def test_the_fingerprint_is_a_sha256_certificate_digest(self):
        statements = self.client.get("/.well-known/assetlinks.json").get_json()
        fingerprints = statements[0]["target"]["sha256_cert_fingerprints"]
        self.assertTrue(fingerprints)
        for fingerprint in fingerprints:
            octets = fingerprint.split(":")
            self.assertEqual(len(octets), 32, f"not a SHA-256 digest: {fingerprint}")
            for octet in octets:
                self.assertEqual(len(octet), 2)
                int(octet, 16)

    def test_it_is_not_gated_behind_the_login_redirect(self):
        # A redirect here reads to Android as a failed verification.
        response = self.client.get("/.well-known/assetlinks.json")
        self.assertNotIn(response.status_code, (301, 302, 303, 307, 308))
        json.loads(response.get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
