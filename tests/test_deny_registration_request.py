import os
import tempfile
import unittest

import app


class DenyRegistrationRequestTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmpdir = tempfile.TemporaryDirectory()
        cls._db_path = os.path.join(cls._tmpdir.name, "deny_registration_request.sqlite")
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

    def _create_user(self, username, display_name, *, is_admin=False, is_active=True):
        user = app.User(
            username=username,
            display_name=display_name,
            password_hash="hashed",
            is_admin=is_admin,
            is_active=is_active,
        )
        app.db.session.add(user)
        app.db.session.flush()
        return user

    def _create_pending_registration(self, username, display_name, requested_pod, *, with_player=True):
        user = self._create_user(username, display_name, is_active=False)
        if with_player:
            user.player = app.Player(name=display_name)
            app.db.session.flush()

        registration_request = app.RegistrationRequest(
            user_id=user.id,
            requested_pod_id=requested_pod.id,
            status="pending",
        )
        app.db.session.add(registration_request)
        app.db.session.flush()
        return user, registration_request

    def test_podmaster_deny_active_pod_request_preserves_records(self):
        with app.app.app_context():
            requested_pod = app.Pod(name="Active Pod", slug="active-pod", is_active=True)
            app.db.session.add(requested_pod)

            podmaster_user = self._create_user("podmaster", "Podmaster", is_admin=False, is_active=True)
            podmaster_user.player = app.Player(name="Podmaster")
            app.db.session.flush()
            app.db.session.add(
                app.PodMembership(
                    pod_id=requested_pod.id,
                    player_id=podmaster_user.player.id,
                    role="podmaster",
                )
            )

            denied_user, registration_request = self._create_pending_registration(
                "pending-user", "Pending User", requested_pod
            )
            app.db.session.commit()

            client = app.app.test_client()
            with client.session_transaction() as session:
                session["user_id"] = podmaster_user.id

            response = client.post(f"/registration_requests/{registration_request.id}/deny")
            self.assertEqual(response.status_code, 302)
            self.assertIn("/registration_requests", response.headers["Location"])

            app.db.session.refresh(denied_user)
            app.db.session.refresh(registration_request)

            self.assertFalse(denied_user.is_active)
            self.assertEqual(registration_request.status, "denied")
            self.assertIsNotNone(registration_request.reviewed_at)
            self.assertEqual(registration_request.reviewed_by_user_id, podmaster_user.id)
            self.assertEqual(registration_request.user_id, denied_user.id)
            self.assertIsNotNone(denied_user.player)
            self.assertEqual(
                app.RegistrationRequest.query.filter_by(user_id=denied_user.id).count(),
                1,
            )

    def test_admin_deny_inactive_pod_request_allowed_and_redirects_admin_users(self):
        with app.app.app_context():
            inactive_pod = app.Pod(name="Inactive Pod", slug="inactive-pod", is_active=False)
            app.db.session.add(inactive_pod)
            admin_user = self._create_user("admin", "Admin", is_admin=True, is_active=True)
            denied_user, registration_request = self._create_pending_registration(
                "pending-inactive", "Pending Inactive", inactive_pod
            )
            app.db.session.commit()

            client = app.app.test_client()
            with client.session_transaction() as session:
                session["user_id"] = admin_user.id

            response = client.post(f"/admin/users/{denied_user.id}/deny")
            self.assertEqual(response.status_code, 302)
            self.assertIn("/admin/users", response.headers["Location"])

            app.db.session.refresh(denied_user)
            app.db.session.refresh(registration_request)
            self.assertFalse(denied_user.is_active)
            self.assertEqual(registration_request.status, "denied")
            self.assertEqual(registration_request.reviewed_by_user_id, admin_user.id)
            self.assertEqual(app.User.query.filter_by(id=denied_user.id).count(), 1)
            self.assertEqual(app.RegistrationRequest.query.filter_by(id=registration_request.id).count(), 1)

    def test_podmaster_cannot_deny_inactive_pod_request(self):
        with app.app.app_context():
            inactive_pod = app.Pod(name="Dormant Pod", slug="dormant-pod", is_active=False)
            app.db.session.add(inactive_pod)
            podmaster_user = self._create_user("pm2", "Podmaster Two", is_active=True)
            podmaster_user.player = app.Player(name="Podmaster Two")
            app.db.session.flush()

            denied_user, registration_request = self._create_pending_registration(
                "pending-dormant", "Pending Dormant", inactive_pod
            )
            app.db.session.commit()

            client = app.app.test_client()
            with client.session_transaction() as session:
                session["user_id"] = podmaster_user.id

            response = client.post(f"/registration_requests/{registration_request.id}/deny")
            self.assertEqual(response.status_code, 302)
            self.assertIn("/registration_requests", response.headers["Location"])

            app.db.session.refresh(denied_user)
            app.db.session.refresh(registration_request)
            self.assertEqual(registration_request.status, "pending")
            self.assertIsNone(registration_request.reviewed_at)
            self.assertTrue(app.User.query.filter_by(id=denied_user.id).first() is not None)

    def test_admin_and_podmaster_deny_share_same_lifecycle_behavior(self):
        with app.app.app_context():
            active_pod = app.Pod(name="Shared Pod", slug="shared-pod", is_active=True)
            app.db.session.add(active_pod)

            admin_user = self._create_user("admin2", "Admin Two", is_admin=True, is_active=True)
            podmaster_user = self._create_user("pm3", "Podmaster Three", is_active=True)
            podmaster_user.player = app.Player(name="Podmaster Three")
            app.db.session.flush()
            app.db.session.add(
                app.PodMembership(
                    pod_id=active_pod.id,
                    player_id=podmaster_user.player.id,
                    role="podmaster",
                )
            )

            denied_by_admin, request_for_admin = self._create_pending_registration(
                "pending-admin-flow", "Pending Admin Flow", active_pod
            )
            denied_by_podmaster, request_for_podmaster = self._create_pending_registration(
                "pending-podmaster-flow", "Pending Podmaster Flow", active_pod
            )
            app.db.session.commit()

            client = app.app.test_client()
            with client.session_transaction() as session:
                session["user_id"] = admin_user.id
            admin_response = client.post(f"/admin/users/{denied_by_admin.id}/deny")
            self.assertEqual(admin_response.status_code, 302)

            with client.session_transaction() as session:
                session["user_id"] = podmaster_user.id
            podmaster_response = client.post(f"/registration_requests/{request_for_podmaster.id}/deny")
            self.assertEqual(podmaster_response.status_code, 302)

            app.db.session.refresh(request_for_admin)
            app.db.session.refresh(request_for_podmaster)

            self.assertEqual(request_for_admin.status, "denied")
            self.assertEqual(request_for_podmaster.status, "denied")
            self.assertEqual(request_for_admin.user_id, denied_by_admin.id)
            self.assertEqual(request_for_podmaster.user_id, denied_by_podmaster.id)
            self.assertEqual(app.User.query.filter_by(id=denied_by_admin.id).count(), 1)
            self.assertEqual(app.User.query.filter_by(id=denied_by_podmaster.id).count(), 1)


if __name__ == "__main__":
    unittest.main()
