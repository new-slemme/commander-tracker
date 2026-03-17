import os
import tempfile
import unittest

import app


class ApiPodsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmpdir = tempfile.TemporaryDirectory()
        cls._db_path = os.path.join(cls._tmpdir.name, "api_pods.sqlite")
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

    def _auth_client_for(self, user_id):
        client = app.app.test_client()
        with client.session_transaction() as session:
            session["user_id"] = user_id
        return client

    def _seed_pod_world(self):
        default_pod = app.Pod(name=app.DEFAULT_POD_NAME, slug=app.DEFAULT_POD_SLUG, is_active=True)
        side_pod = app.Pod(name="Side Pod", slug="side-pod", is_active=True)
        retired_pod = app.Pod(name="Retired Pod", slug="retired-pod", is_active=False)
        app.db.session.add_all([default_pod, side_pod, retired_pod])
        app.db.session.flush()

        admin_user = app.User(username="admin", display_name="Admin", password_hash="x", is_active=True, is_admin=True)
        podmaster_user = app.User(username="pm", display_name="Podmaster", password_hash="x", is_active=True, is_admin=False)
        member_user = app.User(username="member", display_name="Member", password_hash="x", is_active=True, is_admin=False)
        outsider_user = app.User(username="outsider", display_name="Outsider", password_hash="x", is_active=True, is_admin=False)
        app.db.session.add_all([admin_user, podmaster_user, member_user, outsider_user])
        app.db.session.flush()

        admin_player = app.Player(name="Admin Player", user_id=admin_user.id)
        podmaster_player = app.Player(name="Podmaster Player", user_id=podmaster_user.id)
        member_player = app.Player(name="Member Player", user_id=member_user.id)
        outsider_player = app.Player(name="Outsider Player", user_id=outsider_user.id)
        free_player = app.Player(name="Free Agent")
        app.db.session.add_all([admin_player, podmaster_player, member_player, outsider_player, free_player])
        app.db.session.flush()

        app.db.session.add_all([
            app.PodMembership(pod_id=default_pod.id, player_id=admin_player.id, role="podmaster"),
            app.PodMembership(pod_id=default_pod.id, player_id=podmaster_player.id, role="podmaster"),
            app.PodMembership(pod_id=default_pod.id, player_id=member_player.id, role="member"),
            app.PodMembership(pod_id=side_pod.id, player_id=podmaster_player.id, role="podmaster"),
            app.PodMembership(pod_id=side_pod.id, player_id=member_player.id, role="member"),
        ])
        app.db.session.commit()
        return {
            "default_pod": default_pod,
            "side_pod": side_pod,
            "retired_pod": retired_pod,
            "admin_user": admin_user,
            "podmaster_user": podmaster_user,
            "member_user": member_user,
            "outsider_user": outsider_user,
            "admin_player": admin_player,
            "podmaster_player": podmaster_player,
            "member_player": member_player,
            "outsider_player": outsider_player,
            "free_player": free_player,
        }

    def test_list_and_switch_pods_for_member(self):
        with app.app.app_context():
            seeded = self._seed_pod_world()
            client = self._auth_client_for(seeded["member_user"].id)

            list_response = client.get("/api/pods")
            self.assertEqual(list_response.status_code, 200)
            payload = list_response.get_json()
            self.assertEqual(len(payload["pods"]), 2)
            self.assertTrue(any(pod["is_active_selection"] for pod in payload["pods"]))

            switch_response = client.post(f"/api/pods/{seeded['side_pod'].id}/switch")
            self.assertEqual(switch_response.status_code, 200)
            switch_payload = switch_response.get_json()
            self.assertEqual(switch_payload["active_pod_id"], seeded["side_pod"].id)

    def test_admin_can_create_retire_restore_and_delete_pod(self):
        with app.app.app_context():
            seeded = self._seed_pod_world()
            client = self._auth_client_for(seeded["admin_user"].id)

            create_response = client.post("/api/pods", json={"name": "Fresh Pod", "slug": "fresh-pod"})
            self.assertEqual(create_response.status_code, 201)
            created = create_response.get_json()
            created_id = created["id"]
            self.assertGreaterEqual(created["member_count"], 5)

            retire_response = client.post(f"/api/pods/{created_id}/retire")
            self.assertEqual(retire_response.status_code, 200)
            self.assertFalse(retire_response.get_json()["is_active"])

            restore_response = client.post(f"/api/pods/{created_id}/restore")
            self.assertEqual(restore_response.status_code, 200)
            self.assertTrue(restore_response.get_json()["is_active"])

            delete_response = client.delete(f"/api/pods/{created_id}")
            self.assertEqual(delete_response.status_code, 200)
            self.assertEqual(delete_response.get_json()["ok"], True)

    def test_podmaster_can_manage_members_but_not_promote_to_podmaster(self):
        with app.app.app_context():
            seeded = self._seed_pod_world()
            client = self._auth_client_for(seeded["podmaster_user"].id)

            add_response = client.post(
                f"/api/pods/{seeded['side_pod'].id}/members",
                json={"player_id": seeded["free_player"].id, "role": "podmaster"},
            )
            self.assertEqual(add_response.status_code, 200)
            added_member = next(
                member for member in add_response.get_json()["members"]
                if member["player_id"] == seeded["free_player"].id
            )
            self.assertEqual(added_member["role"], "member")

            promote_response = client.patch(
                f"/api/pods/{seeded['side_pod'].id}/members/{seeded['member_player'].id}",
                json={"role": "podmaster"},
            )
            self.assertEqual(promote_response.status_code, 403)

    def test_removing_last_podmaster_requires_admin(self):
        with app.app.app_context():
            seeded = self._seed_pod_world()
            client = self._auth_client_for(seeded["podmaster_user"].id)

            remove_other_response = client.delete(
                f"/api/pods/{seeded['default_pod'].id}/members/{seeded['admin_player'].id}"
            )
            self.assertEqual(remove_other_response.status_code, 200)

            remove_last_response = client.delete(
                f"/api/pods/{seeded['default_pod'].id}/members/{seeded['podmaster_player'].id}"
            )
            self.assertEqual(remove_last_response.status_code, 409)
            self.assertIn("At least one podmaster must remain", remove_last_response.get_json()["error"])

    def test_non_member_cannot_access_pod_detail(self):
        with app.app.app_context():
            seeded = self._seed_pod_world()
            client = self._auth_client_for(seeded["outsider_user"].id)

            response = client.get(f"/api/pods/{seeded['side_pod'].id}")
            self.assertEqual(response.status_code, 403)


if __name__ == "__main__":
    unittest.main()
