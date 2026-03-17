import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app


class AndroidReleaseManifestTests(unittest.TestCase):
    def test_prefers_newest_apk_in_directory_when_manifest_is_stale(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            apk_dir = Path(tmpdir)
            old_apk = apk_dir / "edh-son-0.6.2+8.apk"
            new_apk = apk_dir / "edh-son-0.6.3+9.apk"
            manifest_path = apk_dir / "android-latest.json"

            old_apk.write_bytes(b"old")
            new_apk.write_bytes(b"new")
            os.utime(old_apk, (100, 100))
            os.utime(new_apk, (200, 200))
            manifest_path.write_text(
                json.dumps(
                    {
                        "versionCode": 8,
                        "versionName": "0.6.2",
                        "artifactFileName": old_apk.name,
                    }
                ),
                encoding="utf-8",
            )

            with patch.object(app, "APK_DIR", apk_dir), patch.object(app, "ANDROID_LATEST_RELEASE_MANIFEST", manifest_path):
                with app.app.test_request_context():
                    payload, error = app._load_android_release_manifest()

            self.assertIsNone(error)
            assert payload is not None
            self.assertEqual(payload["artifactFileName"], new_apk.name)
            self.assertEqual(payload["versionName"], "0.6.3")
            self.assertEqual(payload["versionCode"], 9)

    def test_uses_manifest_metadata_when_it_matches_latest_apk(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            apk_dir = Path(tmpdir)
            apk_path = apk_dir / "edh-son-0.6.2+8.apk"
            manifest_path = apk_dir / "android-latest.json"

            apk_path.write_bytes(b"apk")
            os.utime(apk_path, (200, 200))
            manifest_path.write_text(
                json.dumps(
                    {
                        "applicationId": "de.slemme.edhcompanion",
                        "versionCode": 8,
                        "versionName": "0.6.2",
                        "artifactFileName": apk_path.name,
                        "notes": "stable",
                    }
                ),
                encoding="utf-8",
            )

            with patch.object(app, "APK_DIR", apk_dir), patch.object(app, "ANDROID_LATEST_RELEASE_MANIFEST", manifest_path):
                with app.app.test_request_context():
                    payload, error = app._load_android_release_manifest()

            self.assertIsNone(error)
            assert payload is not None
            self.assertEqual(payload["artifactFileName"], apk_path.name)
            self.assertEqual(payload["applicationId"], "de.slemme.edhcompanion")
            self.assertEqual(payload["notes"], "stable")
