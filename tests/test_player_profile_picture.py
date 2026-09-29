import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import app


# Two 1x1 frames with different palette indices; animation must survive unchanged.
ANIMATED_GIF = bytes.fromhex(
    '47494638396101000100800000ff00000000ff'
    '21ff0b4e45545343415045322e300301000000'
    '21f90400640000002c0000000001000100000202440100'
    '21f90400640000002c00000000010001000002024c01003b'
)


class PlayerProfilePictureTests(unittest.TestCase):
    def setUp(self):
        app.app.config['TESTING'] = True
        self.ctx = app.app.app_context()
        self.ctx.push()
        app.db.session.remove()
        app.db.drop_all()
        app.db.create_all()
        self.tmp = tempfile.TemporaryDirectory()
        self.art = Path(self.tmp.name)
        self.patches = [patch.object(app, 'ART_DIR', self.art),
                        patch.object(app, 'get_object_storage_client', return_value=None)]
        for mock in self.patches:
            mock.start()
        self.addCleanup(self.cleanup)
        self.owner = app.User(username='picture-owner', display_name='Picture Owner',
                              password_hash='unused', is_active=True)
        self.other = app.User(username='picture-other', display_name='Other Player',
                              password_hash='unused', is_active=True)
        self.admin = app.User(username='picture-admin', display_name='Admin',
                              password_hash='unused', is_active=True, is_admin=True)
        app.db.session.add_all([self.owner, self.other, self.admin])
        app.db.session.flush()
        self.player = app.Player(name='Picture Owner', user_id=self.owner.id)
        self.peer = app.Player(name='Other Player', user_id=self.other.id)
        self.guest = app.Player(name='Picture Guest')
        app.db.session.add_all([self.player, self.peer, self.guest])
        app.db.session.flush()
        pod = app.Pod(name='Picture Test Pod', slug='picture-test', is_active=True)
        app.db.session.add(pod)
        app.db.session.flush()
        for player in (self.player, self.peer, self.guest):
            app.db.session.add(app.PodMembership(player_id=player.id, pod_id=pod.id, role='member'))
        self.deck = app.Deck(name='Pirates', commander='Admiral Beckett Brass',
                             player_id=self.player.id, commander_art_crop_url='https://example.com/brass.jpg')
        self.other_deck = app.Deck(name='Other Deck', commander='Sol Ring', player_id=self.peer.id)
        app.db.session.add_all([self.deck, self.other_deck])
        app.db.session.commit()
        self.client = app.app.test_client()
        self.login(self.owner)
        self.url = f'/api/players/{self.player.id}/profile-picture'

    def cleanup(self):
        app.db.session.remove()
        self.ctx.pop()
        for mock in reversed(self.patches):
            mock.stop()
        self.tmp.cleanup()

    def login(self, user):
        with self.client.session_transaction() as session:
            session.clear()
            session['user_id'] = user.id

    def upload(self, content=ANIMATED_GIF, filename='avatar.GIF', url=None):
        return self.client.post(url or self.url,
                                data={'file': (io.BytesIO(content), filename)})

    def test_animated_gif_round_trip_and_api_fields(self):
        response = self.upload()
        self.assertEqual(response.status_code, 200)
        url = response.json['profile_picture_url']
        self.assertTrue(url.endswith('.gif'))
        served = self.client.get(url)
        self.assertEqual(served.data, ANIMATED_GIF)
        self.assertEqual(served.mimetype, 'image/gif')
        self.assertEqual(self.client.get('/api/me').json['profile_picture_url'], url)
        detail = self.client.get(f'/api/players/{self.player.id}').json
        self.assertEqual(detail['profile_picture_url'], url)
        self.assertTrue(detail['can_edit_picture'])
        self.assertEqual(detail['picture_choices'][0]['deck_id'], self.deck.id)
        players = self.client.get('/api/players').json
        self.assertEqual(next(p for p in players if p['id'] == self.player.id)['profile_picture_url'], url)

    def test_select_own_commander_and_remove(self):
        response = self.client.post(self.url, json={'deck_id': self.deck.id})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json['profile_picture_url'], self.deck.commander_art_url)
        self.assertIsNone(self.client.delete(self.url).json['profile_picture_url'])

    def test_partner_commander_is_selectable(self):
        self.deck.commander = 'Tymna the Weaver + Thrasios, Triton Hero'
        app.db.session.commit()
        choices = app.player_picture_choices(self.player)
        self.assertEqual([c['commander_index'] for c in choices], [0, 1])
        with patch.object(app, 'resolve_commander_metadata', return_value={'commander_art_crop_url': 'https://example.com/thrasios.jpg'}) as resolve:
            response = self.client.post(self.url, json={'deck_id': self.deck.id, 'commander_index': 1})
        self.assertEqual(response.status_code, 200)
        resolve.assert_called_once_with('Thrasios, Triton Hero')
        self.assertEqual(response.json['profile_picture_url'], 'https://example.com/thrasios.jpg')

    def test_cannot_select_another_players_deck(self):
        self.assertEqual(self.client.post(self.url, json={'deck_id': self.other_deck.id}).status_code, 400)
        self.assertIsNone(self.player.profile_picture_url)

    def test_peer_cannot_change_picture(self):
        self.login(self.other)
        self.assertEqual(self.upload().status_code, 403)
        self.assertEqual(self.client.delete(self.url).status_code, 403)
        self.assertFalse(self.client.get(f'/api/players/{self.player.id}').json['can_edit_picture'])

    def test_outside_pod_is_hidden(self):
        app.PodMembership.query.filter_by(player_id=self.peer.id).delete()
        app.db.session.commit()
        self.login(self.other)
        self.assertEqual(self.upload().status_code, 404)

    def test_anonymous_cannot_upload(self):
        with self.client.session_transaction() as session:
            session.clear()
        self.assertEqual(self.upload().status_code, 401)

    def test_admin_can_set_guest_picture(self):
        self.login(self.admin)
        url = f'/api/players/{self.guest.id}/profile-picture'
        self.assertEqual(self.upload(url=url).status_code, 200)

    def test_invalid_uploads_leave_existing_picture_unchanged(self):
        original = self.upload().json['profile_picture_url']
        for content, name in [(b'', 'empty.gif'), (b'not an image', 'fake.gif'),
                              (b'<svg></svg>', 'bad.svg'), (ANIMATED_GIF, 'file.html')]:
            with self.subTest(name=name):
                self.assertEqual(self.upload(content, name).status_code, 400)
                self.assertEqual(self.player.profile_picture_url, original)
        self.assertEqual(len(list(self.art.iterdir())), 1)

    def test_invalid_json_and_choices(self):
        for payload in ([], None, {'deck_id': True}, {'deck_id': [1]}, {'deck_id': 'no'},
                        {'deck_id': '9' * 5000}, {'deck_id': '\u00b2'},
                        {'deck_id': self.deck.id, 'commander_index': -1},
                        {'deck_id': self.deck.id, 'commander_index': 100},
                        {'action': 'upload'}, {'action': 'unknown'}):
            with self.subTest(payload=payload):
                response = self.client.post(self.url, json=payload)
                self.assertEqual(response.status_code, 400)

    def test_size_limit(self):
        with patch.dict(app.app.config, {'MAX_CONTENT_LENGTH': 512}):
            response = self.upload(ANIMATED_GIF + b'0' * 1024)
        self.assertEqual(response.status_code, 413)
        self.assertIn('too large', response.json['error'])
        self.assertEqual(list(self.art.iterdir()), [])

    def test_replacement_and_removal_prune_uploads(self):
        first = self.upload().json['profile_picture_url']
        second = self.upload().json['profile_picture_url']
        self.assertNotEqual(first, second)
        self.assertFalse((self.art / first.removeprefix('/art/')).exists())
        self.assertTrue((self.art / second.removeprefix('/art/')).exists())
        self.client.delete(self.url)
        self.assertEqual(list(self.art.iterdir()), [])

    def test_deck_picture_is_retained_while_player_uses_it(self):
        url = self.upload().json['profile_picture_url']
        self.deck.commander_local_art_custom = url
        app.db.session.commit()
        self.client.delete(self.url)
        path = self.art / url.removeprefix('/art/')
        self.assertTrue(path.exists())
        self.client.post(self.url, json={'deck_id': self.deck.id})
        self.deck.commander_local_art_custom = None
        app.db.session.commit()
        app.prune_custom_art_file(url)
        self.assertTrue(path.exists())
        self.client.delete(self.url)
        self.assertFalse(path.exists())

    def test_object_storage_keeps_gif_and_content_type(self):
        storage = Mock()
        with patch.object(app, 'get_object_storage_client', return_value=storage):
            response = self.upload()
            self.assertEqual(response.status_code, 200)
            kwargs = storage.put_object.call_args.kwargs
            self.assertEqual(kwargs['Body'], ANIMATED_GIF)
            self.assertEqual(kwargs['ContentType'], 'image/gif')
            self.assertTrue(response.json['profile_picture_url'].startswith('/media/custom-art/profile/'))
            self.client.delete(self.url)
            storage.delete_object.assert_called_once()

    def test_unavailable_commander_keeps_current_picture(self):
        original = self.upload().json['profile_picture_url']
        self.deck.commander_art_crop_url = None
        app.db.session.commit()
        with patch.object(app, 'resolve_commander_metadata', return_value={}):
            response = self.client.post(self.url, json={'deck_id': self.deck.id})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.player.profile_picture_url, original)

    def test_failed_commit_removes_new_upload(self):
        original = self.upload().json['profile_picture_url']
        with patch.object(app.db.session, 'commit', side_effect=RuntimeError('commit failed')):
            with self.assertRaisesRegex(RuntimeError, 'commit failed'):
                self.upload()
        self.assertEqual(self.player.profile_picture_url, original)
        self.assertEqual(len(list(self.art.iterdir())), 1)

    def test_guest_deletion_prunes_picture(self):
        self.login(self.admin)
        guest_url = f'/api/players/{self.guest.id}'
        url = self.upload(url=guest_url+'/profile-picture').json['profile_picture_url']
        self.assertEqual(self.client.delete(guest_url).status_code, 200)
        self.assertFalse((self.art / url.removeprefix('/art/')).exists())

    def test_account_anonymization_clears_picture(self):
        self.owner.password_hash = app.generate_password_hash('picture-test-password')
        app.db.session.commit()
        url = self.upload().json['profile_picture_url']
        self.assertEqual(app.build_account_export(self.owner)['account']['profile_picture_url'], url)
        with self.client.session_transaction() as session:
            session['_csrf_token'] = 'picture-test-token'
        response = self.client.post('/profile', data={'action': 'delete_account',
                                   'current_password': 'picture-test-password',
                                   '_csrf_token': 'picture-test-token'})
        self.assertEqual(response.status_code, 302)
        self.assertIsNone(self.player.profile_picture_url)
        self.assertFalse((self.art / url.removeprefix('/art/')).exists())

    def test_web_form_csrf_and_profile_return(self):
        web_url = f'/player/{self.player.id}/profile-picture'
        with self.client.session_transaction() as session:
            session['_csrf_token'] = 'picture-test-token'
        with patch.dict(app.app.config, {'WTF_CSRF_ENABLED': True}):
            self.assertEqual(self.client.post(web_url, data={'action': 'remove'}).status_code, 403)
        response = self.client.post(web_url, data={'action': 'commander', 'deck_id': self.deck.id,
                                   '_csrf_token': 'picture-test-token', 'return_to': 'profile'})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.location, '/profile')
        page = self.client.get('/profile')
        self.assertEqual(page.status_code, 200)
        self.assertIn(b'Animated GIFs stay animated', page.data)
        self.assertIn(self.deck.commander_art_url.encode(), page.data)
        self.assertEqual(self.client.get(f'/player/{self.player.id}').status_code, 200)
        self.assertIn(self.deck.commander_art_url.encode(), self.client.get('/players').data)

    def test_migration_upgrades_existing_players_and_is_idempotent(self):
        app.db.session.execute(app.text('ALTER TABLE player DROP COLUMN profile_picture_url'))
        app.db.session.execute(app.text("DELETE FROM schema_migrations WHERE version = '028_player_profile_picture'"))
        app.db.session.commit()
        app.run_schema_migrations()
        app.run_schema_migrations()
        app.db.session.expire_all()
        self.assertEqual(self.player.name, 'Picture Owner')
        self.assertIsNone(self.player.profile_picture_url)


if __name__ == '__main__':
    unittest.main()
