import io
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

import app
from commander_media import MediaError, convert_animation, get_variants
from tests.test_player_profile_picture import ANIMATED_GIF


@unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'FFmpeg is required')
class CommanderMediaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.video, cls.poster = convert_animation(ANIMATED_GIF, '.gif')

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / 'commander_art').mkdir()
        (self.root / 'card_art').mkdir()
        for name, value in [('ART_DIR', self.root), ('COMMANDER_ART_DIR', self.root / 'commander_art'),
                            ('CARD_ART_DIR', self.root / 'card_art')]:
            mock = patch.object(app, name, value)
            mock.start()
            self.addCleanup(mock.stop)
        self.storage = patch.object(app, 'get_object_storage_client', return_value=None)
        self.storage.start()
        self.addCleanup(self.storage.stop)
        self.ctx = app.app.app_context()
        self.ctx.push()
        self.addCleanup(self.ctx.pop)
        self.addCleanup(app.db.session.remove)
        app.db.drop_all()
        app.db.create_all()
        user = app.User(username='motion-owner', display_name='Motion Owner', password_hash='unused', is_active=True)
        app.db.session.add(user)
        app.db.session.flush()
        player = app.Player(name='Motion Owner', user_id=user.id)
        app.db.session.add(player)
        app.db.session.flush()
        self.deck = app.Deck(name='Motion Test', commander='Admiral Brass, Unsinkable', player_id=player.id)
        app.db.session.add(self.deck)
        app.db.session.commit()
        self.client = app.app.test_client()
        with self.client.session_transaction() as session:
            session['user_id'] = user.id
        self.url = f'/api/decks/{self.deck.id}/upload-art'

    def upload(self, data, filename, field='commander'):
        return self.client.post(self.url, data={'field': field, 'file': (io.BytesIO(data), filename)})

    def test_gif_keeps_legacy_url_and_adds_poster_video_and_range_support(self):
        result = self.upload(ANIMATED_GIF, 'test.GIF')
        self.assertEqual(result.status_code, 200, result.json)
        self.assertTrue(result.json['url'].endswith('.gif'))
        self.assertEqual(self.client.get(result.json['url']).data, ANIMATED_GIF)
        self.assertEqual(self.client.get(result.json['poster_url']).mimetype, 'image/jpeg')
        video = self.client.get(result.json['video_url'], headers={'Range': 'bytes=0-99'})
        self.assertEqual(video.status_code, 206)
        self.assertEqual(video.mimetype, 'video/mp4')
        self.assertEqual(len(video.data), 100)
        self.assertEqual(self.deck.commander_art_video_url, result.json['video_url'])
        self.assertEqual(self.deck.commander_art_poster_url, result.json['poster_url'])

    def test_mp4_normalized_to_h264_and_image_fallback(self):
        result = self.upload(self.video, 'test.mp4')
        self.assertEqual(result.status_code, 200, result.json)
        self.assertEqual(result.json['url'], result.json['poster_url'])
        self.assertEqual(self.client.get(result.json['url']).mimetype, 'image/jpeg')
        path = app._local_art_path(result.json['video_url'])
        info = json.loads(subprocess.check_output(['ffprobe', '-v', 'error', '-show_streams', '-of', 'json', str(path)]))
        self.assertEqual(len(info['streams']), 1)
        self.assertEqual(info['streams'][0]['codec_name'], 'h264')
        self.assertEqual(info['streams'][0]['pix_fmt'], 'yuv420p')

    def test_invalid_upload_does_not_replace_previous_art(self):
        original = self.upload(ANIMATED_GIF, 'good.gif').json['url']
        for content, filename, field in [(b'invalid', 'bad.mp4', 'commander'),
                                         (self.video, 'video.mp4', 'card')]:
            response = self.upload(content, filename, field)
            self.assertEqual(response.status_code, 400)
            self.assertEqual(self.deck.commander_art_url, original)

    def test_conversion_failure_is_actionable_and_leaves_no_files(self):
        with patch('app.convert_animation', side_effect=MediaError('install FFmpeg')):
            response = self.upload(ANIMATED_GIF, 'test.gif')
        self.assertEqual(response.status_code, 400)
        self.assertIn('FFmpeg', response.json['error'])
        self.assertEqual(list((self.root / 'commander_art').iterdir()), [])

    def test_limits_and_private_upload_authorization(self):
        with patch('app.MAX_MEDIA_BYTES', len(ANIMATED_GIF) - 1):
            self.assertEqual(self.upload(ANIMATED_GIF, 'big.gif').status_code, 400)
        with self.client.session_transaction() as session:
            session.clear()
        self.assertEqual(self.upload(ANIMATED_GIF, 'test.gif').status_code, 401)

    def test_thirty_mb_file_has_room_for_multipart_overhead(self):
        content = ANIMATED_GIF + b'\0' * (30 * 1024 * 1024 - len(ANIMATED_GIF))
        # Decoder behavior is covered by real FFmpeg tests; this tests the two
        # independent HTTP-request and individual-file byte limits.
        with patch('app.convert_animation', return_value=(self.video, self.poster)):
            result = self.upload(content, 'large.gif')
            self.assertEqual(result.status_code, 200, result.json)
            result = self.upload(content + b'\0', 'too-large.gif')
            self.assertEqual(result.status_code, 400)

    def test_legacy_conversion_is_repeatable_and_preserves_sources_and_records(self):
        source = self.root / 'custom_legacy.gif'
        source.write_bytes(ANIMATED_GIF)
        self.deck.commander_local_art_custom = '/art/custom_legacy.gif'
        app.db.session.commit()
        result = app.convert_legacy_commander_art(self.deck.commander_art_url)
        self.assertEqual(result['status'], 'converted')
        with patch('app.convert_animation', side_effect=AssertionError('must not reconvert')):
            self.assertEqual(app.convert_legacy_commander_art(self.deck.commander_art_url)['status'], 'existing')
        self.assertEqual(source.read_bytes(), ANIMATED_GIF)
        self.assertEqual(self.deck.commander_art_url, '/art/custom_legacy.gif')
        for invalid in ['/art/../outside.gif', 'http://example.com/art.gif']:
            with self.assertRaises(MediaError):
                app.convert_legacy_commander_art(invalid)

    def test_replacement_prunes_derivatives_but_shared_player_picture_survives(self):
        old = self.upload(ANIMATED_GIF, 'test.gif').json
        player = self.deck.owner
        player.profile_picture_url = old['url']
        app.db.session.commit()
        self.upload(self.poster, 'still.jpg')
        self.assertTrue(app._local_art_path(old['video_url']).exists())
        player.profile_picture_url = None
        app.db.session.commit()
        app.prune_custom_art_file(old['url'])
        self.assertFalse(app._local_art_path(old['video_url']).exists())
        self.assertFalse(app._local_art_path(old['poster_url']).exists())
        self.assertEqual(get_variants(self.root, old['url']), {})

    def test_object_storage_variants_have_correct_mime_types(self):
        client = Mock()
        with patch.object(app, 'get_object_storage_client', return_value=client):
            result = self.upload(ANIMATED_GIF, 'remote.gif')
        self.assertEqual(result.status_code, 200, result.json)
        types = [call.kwargs['ContentType'] for call in client.put_object.call_args_list]
        self.assertEqual(types, ['image/gif', 'video/mp4', 'image/jpeg'])
        self.assertTrue(self.deck.commander_art_video_url.startswith('/media/custom-art/commander/'))

    def test_duration_and_dimension_limits(self):
        for width, duration in [(5000, 1), (640, 61)]:
            info = {'streams': [{'codec_type': 'video', 'width': width, 'height': 480}],
                    'format': {'duration': duration}}
            with patch('commander_media._run', return_value=json.dumps(info).encode()):
                with self.assertRaises(MediaError):
                    convert_animation(ANIMATED_GIF, '.gif')


if __name__ == '__main__':
    unittest.main()
