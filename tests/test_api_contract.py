"""Contract tests: docs/API.md must stay in sync with the registered /api routes.

The Android client (~/mine/edh-son-android) is built against docs/API.md. When the
doc drifts from app.url_map the client is built against a contract that does not
exist -- which is exactly what happened between the v1.9.0 APK publish and now.

These tests are the guard that makes ROUTING.md's "keep Android API docs in sync"
rule enforceable instead of advisory.
"""
import os
import re
import unittest

from app import app as flask_app, db


DOCS_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "docs", "API.md")

# The doc's machine-checked endpoint index. Rows look like:
#   | `GET` | `/api/decks/{deck_id}` | auth |
INDEX_ROW_RE = re.compile(
    r"^\|\s*`(?P<method>[A-Z]+)`\s*\|\s*`(?P<path>/api/[^`]*)`\s*\|\s*(?P<auth>[a-z]+)\s*\|",
    re.MULTILINE,
)

# Path params are documented as {name}; Flask registers them as <converter:name>.
FLASK_PARAM_RE = re.compile(r"<(?:[a-zA-Z_]+:)?([a-zA-Z_][a-zA-Z0-9_]*)>")

IGNORED_METHODS = {"HEAD", "OPTIONS"}

VALID_AUTH_VALUES = {"public", "auth", "admin", "token"}


def normalize_rule(rule):
    """/api/decks/<int:deck_id> -> /api/decks/{deck_id}"""
    return FLASK_PARAM_RE.sub(r"{\1}", rule)


def registered_endpoints():
    """{(method, normalized_path)} for every registered /api route."""
    found = set()
    for rule in flask_app.url_map.iter_rules():
        path = str(rule.rule)
        if not path.startswith("/api"):
            continue
        for method in rule.methods - IGNORED_METHODS:
            found.add((method, normalize_rule(path)))
    return found


def documented_endpoints():
    """{(method, path): auth} parsed from the endpoint index table in docs/API.md."""
    with open(DOCS_PATH, encoding="utf-8") as fh:
        content = fh.read()
    documented = {}
    for match in INDEX_ROW_RE.finditer(content):
        key = (match.group("method"), match.group("path"))
        documented[key] = match.group("auth")
    return documented


class ApiDocsCoverageTests(unittest.TestCase):
    """Every registered /api route is documented, and vice versa."""

    def test_docs_file_exists(self):
        self.assertTrue(os.path.isfile(DOCS_PATH), f"missing API reference at {DOCS_PATH}")

    def test_endpoint_index_is_parseable(self):
        documented = documented_endpoints()
        self.assertGreater(
            len(documented), 0,
            "docs/API.md has no parseable endpoint index. Expected rows of the form:\n"
            "  | `GET` | `/api/me` | auth |",
        )

    def test_every_registered_route_is_documented(self):
        missing = sorted(registered_endpoints() - set(documented_endpoints()))
        self.assertEqual(
            missing, [],
            "These routes exist but are not in the docs/API.md endpoint index:\n"
            + "\n".join(f"  {m} {p}" for m, p in missing),
        )

    def test_no_documented_route_is_stale(self):
        stale = sorted(set(documented_endpoints()) - registered_endpoints())
        self.assertEqual(
            stale, [],
            "These routes are documented but no longer registered:\n"
            + "\n".join(f"  {m} {p}" for m, p in stale),
        )

    def test_auth_values_are_valid(self):
        bad = {k: v for k, v in documented_endpoints().items() if v not in VALID_AUTH_VALUES}
        self.assertEqual(
            bad, {},
            f"Auth column must be one of {sorted(VALID_AUTH_VALUES)}; got: {bad}",
        )


class ApiDocsAuthAccuracyTests(unittest.TestCase):
    """The documented auth level must match what the server actually enforces.

    Checked behaviourally rather than by inspecting the public_endpoints set: an
    endpoint can sit in public_endpoints and still be gated by its own
    @api_login_required decorator (api_me does exactly that), so the set alone
    does not tell a client author what to expect.

    Restricted to GET so the probe cannot mutate state or trip the login rate
    limiter. That is enough to cover the drift that actually misled readers --
    /api/cards/prints and /api/ccauto/* were documented as public but return 401.
    """

    @classmethod
    def setUpClass(cls):
        flask_app.config["TESTING"] = True
        # These probes hit real endpoints, some of which touch the DB (the live-game
        # token routes read active_game). Create the schema here rather than relying
        # on whichever test happened to run first leaving tables behind.
        with flask_app.app_context():
            db.create_all()

    def _probe_path(self, path):
        """Fill path params with a value that is syntactically valid but absent."""
        return path.replace("{deck_id}", "999999") \
                   .replace("{game_id}", "999999") \
                   .replace("{player_id}", "999999") \
                   .replace("{user_id}", "999999") \
                   .replace("{pod_id}", "999999") \
                   .replace("{request_id}", "999999") \
                   .replace("{token}", "contract-test-token") \
                   .replace("{set_name}", "contract-test-set")

    def test_documented_auth_matches_enforcement(self):
        # 'public' and 'token' endpoints are both reachable without a session -- a
        # token route authorises on the path token instead, so an unknown token is a
        # 404, not a 401. Only 'auth' and 'admin' are gated by the session.
        session_gated = {"auth", "admin"}
        mismatches = []
        client = flask_app.test_client()  # no session -> unauthenticated
        for (method, path), auth in sorted(documented_endpoints().items()):
            if method != "GET":
                continue
            resp = client.get(self._probe_path(path))
            got_401 = resp.status_code == 401
            expects_401 = auth in session_gated
            if got_401 != expects_401:
                mismatches.append(
                    f"  {method} {path}: documented '{auth}' but unauthenticated "
                    f"request returned {resp.status_code}"
                )
        self.assertEqual(
            mismatches, [],
            "Documented auth level does not match enforced behaviour:\n" + "\n".join(mismatches),
        )


if __name__ == "__main__":
    unittest.main()
