import os
import tempfile
import unittest

import app


class UserIdentityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmpdir = tempfile.TemporaryDirectory()
        cls._db_path = os.path.join(cls._tmpdir.name, "user_identity.sqlite")
        app.app.config.update(
            TESTING=True,
            SQLALCHEMY_DATABASE_URI=f"sqlite:///{cls._db_path}",
            WTF_CSRF_ENABLED=False,
        )

    @classmethod
    def tearDownClass(cls):
        cls._tmpdir.cleanup()

    def setUp(self):
        with app.app.app_context():
            app.db.session.remove()
            app.db.drop_all()
            app.db.create_all()

    def _create_user(self, username, display_name, *, is_admin=False):
        user = app.User(
            username=username,
            display_name=display_name,
            password_hash="hashed",
            is_admin=is_admin,
            is_active=True,
        )
        user.player = app.Player(name=display_name)
        app.db.session.add(user)
        app.db.session.flush()
        return user

    @staticmethod
    def _sign_in(client, user):
        with client.session_transaction() as signed_in:
            signed_in["user_id"] = user.id
            signed_in["username"] = user.username
            signed_in["display_name"] = user.display_name
            signed_in["session_version"] = user.session_version

    def test_profile_updates_username_display_name_player_and_session(self):
        with app.app.app_context():
            user = self._create_user("old-login", "Old Name")
            app.db.session.commit()
            user_id = user.id

            client = app.app.test_client()
            self._sign_in(client, user)
            response = client.post(
                "/profile",
                data={
                    "action": "update_profile",
                    "username": "new-login",
                    "display_name": "New Name",
                    "mana_fucked_salt_value": "1",
                    "misplayed_salt_value": "1",
                },
            )

            self.assertEqual(response.status_code, 302)
            updated = app.db.session.get(app.User, user_id)
            self.assertEqual(updated.username, "new-login")
            self.assertEqual(updated.display_name, "New Name")
            self.assertEqual(updated.player.name, "New Name")
            with client.session_transaction() as signed_in:
                self.assertEqual(signed_in["username"], "new-login")
                self.assertEqual(signed_in["display_name"], "New Name")

    def test_admin_users_page_has_editable_identity_fields(self):
        with app.app.app_context():
            admin = self._create_user("admin", "Admin", is_admin=True)
            target = self._create_user("jerome", "Jatzek")
            app.db.session.commit()

            client = app.app.test_client()
            self._sign_in(client, admin)
            response = client.get("/admin/users")

            self.assertEqual(response.status_code, 200)
            html = response.get_data(as_text=True)
            self.assertIn(f'action="/admin/users/{target.id}/update"', html)
            self.assertIn('name="username"', html)
            self.assertIn('value="jerome"', html)
            self.assertIn('name="display_name"', html)
            self.assertIn('value="Jatzek"', html)
            self.assertIn("Save identity", html)

    def test_admin_update_syncs_account_and_linked_player(self):
        with app.app.app_context():
            admin = self._create_user("admin", "Admin", is_admin=True)
            target = self._create_user("jerome", "Jatzek")
            app.db.session.commit()
            target_id = target.id

            client = app.app.test_client()
            self._sign_in(client, admin)
            response = client.post(
                f"/admin/users/{target_id}/update",
                data={"username": "jerome-renamed", "display_name": "Jerome"},
            )

            self.assertEqual(response.status_code, 302)
            updated = app.db.session.get(app.User, target_id)
            self.assertEqual(updated.username, "jerome-renamed")
            self.assertEqual(updated.display_name, "Jerome")
            self.assertEqual(updated.player.name, "Jerome")

    def test_duplicate_username_is_rejected_case_insensitively(self):
        with app.app.app_context():
            admin = self._create_user("admin", "Admin", is_admin=True)
            self._create_user("AlreadyUsed", "Someone Else")
            target = self._create_user("jerome", "Jatzek")
            app.db.session.commit()
            target_id = target.id

            client = app.app.test_client()
            self._sign_in(client, admin)
            response = client.post(
                f"/admin/users/{target_id}/update",
                data={"username": "alreadyused", "display_name": "Jerome"},
                follow_redirects=True,
            )

            self.assertEqual(response.status_code, 200)
            self.assertIn("That username is already taken.", response.get_data(as_text=True))
            unchanged = app.db.session.get(app.User, target_id)
            self.assertEqual(unchanged.username, "jerome")
            self.assertEqual(unchanged.display_name, "Jatzek")
            self.assertEqual(unchanged.player.name, "Jatzek")

    def test_admin_api_patch_updates_identity(self):
        with app.app.app_context():
            admin = self._create_user("admin", "Admin", is_admin=True)
            target = self._create_user("jerome", "Jatzek")
            app.db.session.commit()
            target_id = target.id

            client = app.app.test_client()
            self._sign_in(client, admin)
            response = client.patch(
                f"/api/admin/users/{target_id}",
                json={"username": "jerome", "display_name": "Jerome"},
            )

            self.assertEqual(response.status_code, 200)
            payload = response.get_json()["user"]
            self.assertEqual(payload["username"], "jerome")
            self.assertEqual(payload["display_name"], "Jerome")
            self.assertEqual(payload["player_name"], "Jerome")


if __name__ == "__main__":
    unittest.main()
