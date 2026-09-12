"""Phase 6: pod branding and rules configuration over JSON.

The `Pod` columns for this (`flavor_name`, `primary_color`, `visibility`, `timezone`,
`recap_public_by_default`, `enabled_mechanics_json`) have existed for a while but
nothing ever read or wrote them -- there is no web UI either. So this is not mirroring
an existing feature; it is defining the contract, and these tests are where the shape
is decided.

`scoring_json` is deliberately read-only: nothing consumes it and no schema is
documented anywhere, so accepting arbitrary writes would be a storage sink rather than
a feature. See TASK-R33.

Validation matters more than usual because these values are rendered: a colour goes
into a theme, a timezone into date formatting, and a flavour name into headings.
"""
import json
import unittest
from datetime import datetime

from werkzeug.security import generate_password_hash

import app as app_module
from app import app as flask_app, db, User, Player, Pod, PodMembership

PASSWORD = "StrongPass123"


class PodConfigApiTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        flask_app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
        flask_app.config["TESTING"] = True
        app_module.limiter.enabled = False
        with flask_app.app_context():
            db.create_all()

    @classmethod
    def tearDownClass(cls):
        with flask_app.app_context():
            db.drop_all()

    def setUp(self):
        self.ctx = flask_app.app_context()
        self.ctx.push()
        for model in (PodMembership, Pod, Player, User):
            db.session.query(model).delete()
        db.session.commit()

        self.owner = self._user("podmaster")
        self.pod = self._pod("Friday Crew", self.owner)
        self.member = self._user("member")
        app_module.ensure_membership(self.pod.id, self.member.player.id, role="member")
        db.session.commit()

        self.client = self._signed_in(self.owner)

    def tearDown(self):
        db.session.remove()
        self.ctx.pop()

    def _user(self, username, is_admin=False):
        user = User(
            username=username,
            display_name=username.title(),
            email=f"{username}@example.test",
            password_hash=generate_password_hash(PASSWORD),
            is_active=True,
            is_admin=is_admin,
            approved_at=datetime.utcnow(),
            email_verified_at=datetime.utcnow(),
        )
        db.session.add(user)
        db.session.flush()
        user.player = Player(name=user.display_name, user_id=user.id)
        db.session.commit()
        return user

    def _pod(self, name, owner):
        pod = Pod(name=name, slug=app_module.unique_pod_slug(name),
                  owner_user_id=owner.id, is_active=True)
        db.session.add(pod)
        db.session.flush()
        app_module.ensure_membership(pod.id, owner.player.id, role="podmaster")
        db.session.commit()
        return pod

    def _signed_in(self, user):
        client = flask_app.test_client()
        response = client.post("/api/login", json={"username": user.username, "password": PASSWORD})
        self.assertEqual(response.status_code, 200)
        return client

    def _patch(self, body, client=None):
        return (client or self.client).patch(f"/api/pods/{self.pod.id}", json=body)

    # ----- reading -------------------------------------------------------

    def test_detail_exposes_the_configuration(self):
        body = self.client.get(f"/api/pods/{self.pod.id}").get_json()
        self.assertEqual(body["flavor_name"], "Saltmine")
        self.assertEqual(body["primary_color"], "#ff7a00")
        self.assertEqual(body["visibility"], "private")
        self.assertEqual(body["timezone"], "UTC")
        self.assertFalse(body["recap_public_by_default"])

    def test_mechanics_default_to_all_enabled(self):
        # An empty column must not read as "every mechanic is switched off", which
        # would silently strip the life counter for every existing pod.
        body = self.client.get(f"/api/pods/{self.pod.id}").get_json()
        self.assertEqual(
            body["enabled_mechanics"],
            {k: True for k in app_module.POD_MECHANIC_KEYS},
        )

    def test_scoring_is_exposed_but_opaque(self):
        self.pod.scoring_json = json.dumps({"win": 3})
        db.session.commit()
        self.assertEqual(self.client.get(f"/api/pods/{self.pod.id}").get_json()["scoring"], {"win": 3})

    def test_a_corrupt_config_column_does_not_break_the_endpoint(self):
        self.pod.enabled_mechanics_json = "{not json"
        self.pod.scoring_json = "also not json"
        db.session.commit()
        body = self.client.get(f"/api/pods/{self.pod.id}").get_json()
        self.assertEqual(body["enabled_mechanics"], {k: True for k in app_module.POD_MECHANIC_KEYS})
        self.assertEqual(body["scoring"], {})

    # ----- writing -------------------------------------------------------

    def test_branding_can_be_changed(self):
        response = self._patch({
            "name": "Friday Crew",
            "flavor_name": "The Salt Mines",
            "primary_color": "#3366FF",
            "recap_public_by_default": True,
        })
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        body = response.get_json()
        self.assertEqual(body["flavor_name"], "The Salt Mines")
        self.assertEqual(body["primary_color"], "#3366ff", "colours normalise to lowercase")
        self.assertTrue(body["recap_public_by_default"])

    def test_omitted_fields_are_left_alone(self):
        self._patch({"name": "Friday Crew", "flavor_name": "Mines"})
        body = self._patch({"name": "Friday Crew", "primary_color": "#112233"}).get_json()
        self.assertEqual(body["flavor_name"], "Mines", "a PATCH must not reset what it omits")

    def test_a_bad_colour_is_refused(self):
        for bad in ("red", "#ff", "#gggggg", "ff7a00", "#ff7a00ff", 42, None):
            with self.subTest(color=bad):
                response = self._patch({"name": "Friday Crew", "primary_color": bad})
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.get_json()["field"], "primary_color")
        db.session.refresh(self.pod)
        self.assertEqual(self.pod.primary_color, "#ff7a00")

    def test_a_three_digit_colour_is_expanded(self):
        body = self._patch({"name": "Friday Crew", "primary_color": "#3af"}).get_json()
        self.assertEqual(body["primary_color"], "#33aaff")

    def test_visibility_is_limited_to_known_values(self):
        for value in app_module.POD_VISIBILITIES:
            with self.subTest(visibility=value):
                body = self._patch({"name": "Friday Crew", "visibility": value}).get_json()
                self.assertEqual(body["visibility"], value)
        response = self._patch({"name": "Friday Crew", "visibility": "world-readable"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["field"], "visibility")

    def test_an_unknown_timezone_is_refused(self):
        good = self._patch({"name": "Friday Crew", "timezone": "Europe/Berlin"})
        self.assertEqual(good.get_json()["timezone"], "Europe/Berlin")
        bad = self._patch({"name": "Friday Crew", "timezone": "Mars/Olympus_Mons"})
        self.assertEqual(bad.status_code, 400)
        self.assertEqual(bad.get_json()["field"], "timezone")

    def test_mechanics_can_be_switched_off_individually(self):
        body = self._patch({
            "name": "Friday Crew",
            "enabled_mechanics": {"poison": False, "energy": False},
        }).get_json()
        self.assertFalse(body["enabled_mechanics"]["poison"])
        self.assertFalse(body["enabled_mechanics"]["energy"])
        self.assertTrue(body["enabled_mechanics"]["monarch"], "unmentioned keys stay on")

    def test_unknown_mechanic_keys_are_refused_rather_than_stored(self):
        response = self._patch({
            "name": "Friday Crew",
            "enabled_mechanics": {"teleportation": True},
        })
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["field"], "enabled_mechanics")

    def test_a_non_boolean_mechanic_value_is_refused(self):
        response = self._patch({"name": "Friday Crew", "enabled_mechanics": {"poison": "yes"}})
        self.assertEqual(response.status_code, 400)

    def test_mechanics_must_be_an_object(self):
        response = self._patch({"name": "Friday Crew", "enabled_mechanics": ["poison"]})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["field"], "enabled_mechanics")

    def test_scoring_cannot_be_written_through_this_endpoint(self):
        response = self._patch({"name": "Friday Crew", "scoring": {"win": 5}})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["field"], "scoring")
        db.session.refresh(self.pod)
        self.assertEqual(self.pod.scoring_json, "{}")

    def test_an_overlong_flavor_name_is_refused(self):
        response = self._patch({"name": "Friday Crew", "flavor_name": "f" * 500})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["field"], "flavor_name")

    def test_an_ordinary_member_cannot_reconfigure_the_pod(self):
        response = self._patch({"name": "Friday Crew", "flavor_name": "Mine Now"}, self._signed_in(self.member))
        self.assertEqual(response.status_code, 403)
        db.session.refresh(self.pod)
        self.assertEqual(self.pod.flavor_name, "Saltmine")

    def test_renaming_still_works_as_before(self):
        body = self._patch({"name": "Saturday Crew"}).get_json()
        self.assertEqual(body["name"], "Saturday Crew")


if __name__ == "__main__":
    unittest.main()
