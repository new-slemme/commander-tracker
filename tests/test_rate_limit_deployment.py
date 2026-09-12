"""TASK-R26: the limiter's storage and the worker count are one decision.

Flask-Limiter's `memory://` store is per-process. Deployed behind several gunicorn
worker processes, each keeps its own counter and every documented limit is silently
multiplied by the worker count -- ~20/hour where docs/API.md promises 5.

That cannot be caught by the test client, which is a single process: in-process the
limits behave exactly as advertised. So this pins the *deployment* invariant instead.
If someone raises the worker count for throughput, this fails and points them at the
storage backend they also have to change.
"""
import re
import unittest
from pathlib import Path

import app as app_module

DOCKERFILE = Path(__file__).resolve().parent.parent / "Dockerfile"
API_DOCS = Path(__file__).resolve().parent.parent / "docs" / "API.md"

WORKERS_RE = re.compile(r'"-w"\s*,\s*"(\d+)"')
THREADS_RE = re.compile(r'"--threads"\s*,\s*"(\d+)"')


class RateLimitDeploymentTests(unittest.TestCase):

    def setUp(self):
        self.dockerfile = DOCKERFILE.read_text(encoding="utf-8")

    def _storage_is_process_local(self) -> bool:
        uri = app_module.limiter._storage_uri or ""
        return uri.startswith("memory://")

    def test_process_local_limiter_storage_requires_a_single_worker(self):
        workers = WORKERS_RE.search(self.dockerfile)
        self.assertIsNotNone(workers, "could not find the gunicorn worker count")
        if not self._storage_is_process_local():
            self.skipTest("limiter storage is shared, so the worker count is free")
        self.assertEqual(
            int(workers.group(1)), 1,
            "memory:// limiter storage is per-process: with N workers every rate limit "
            "in docs/API.md is really N times looser. Either keep -w 1 (and use "
            "--threads for concurrency) or move limiter storage to a shared backend.",
        )

    def test_a_single_worker_still_serves_requests_concurrently(self):
        # -w 1 alone would serialise every request behind one thread, which a slow
        # deck import from a user-supplied URL would then block.
        threads = THREADS_RE.search(self.dockerfile)
        self.assertIsNotNone(
            threads, "with one worker, --threads is what provides concurrency"
        )
        self.assertGreater(int(threads.group(1)), 1)

    def test_the_documented_limits_are_the_ones_the_code_declares(self):
        """Guards the other direction: docs promising a limit the code does not set."""
        table_row = re.compile(
            r"\|\s*`(?:POST|GET) (/api/[^`]+)`\s*\|\s*(\d+)\s*/\s*(minute|hour)\s*\|"
        )
        documented = {
            path: f"{amount} per {period}"
            for path, amount, period in table_row.findall(
                API_DOCS.read_text(encoding="utf-8")
            )
        }
        self.assertTrue(documented, "no rate-limit table found in docs/API.md")

        declared = {}
        for key, route_limits in app_module.limiter.limit_manager._decorated_limits.items():
            endpoint = key.rsplit(".", 1)[-1]
            rule = next(
                (r.rule for r in app_module.app.url_map.iter_rules() if r.endpoint == endpoint),
                None,
            )
            if rule is None:
                continue
            for route_limit in route_limits:
                declared[rule] = "".join(route_limit.limit_provider)

        for path, documented_limit in documented.items():
            with self.subTest(path=path):
                self.assertIn(path, declared, f"{path} documents a limit it does not declare")
                self.assertEqual(
                    declared[path], documented_limit,
                    f"{path}: docs say {documented_limit}, code says {declared[path]}",
                )


if __name__ == "__main__":
    unittest.main()
