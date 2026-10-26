import json
import unittest
from datetime import datetime
from unittest.mock import patch
from werkzeug.security import generate_password_hash, check_password_hash
import app as m
from tests import test_browsing as fixtures
import game_timers


class MobileParityTests(unittest.TestCase):
    setUp = fixtures.BrowsingTests.setUp
    deck = fixtures.BrowsingTests.deck
    game = fixtures.BrowsingTests.game
    login = fixtures.BrowsingTests.login
    read = fixtures.BrowsingTests.read

    def test_web_and_mobile_identical_browsing(self):
        queries = {
            'decks': ['', 'q=Red', 'status=all', 'status=retired', 'status=planned', 'color=R&color=U&color_mode=exact',
                'color=U&color_mode=within', 'bracket=none&bracket=3', 'strategy=tokens&mechanic=poison',
                'freshness=stale', 'freshness=partial', 'decklist=no', 'min_games=2&max_winrate=90',
                'period=custom&date_from=2026-10-22&date_to=2026-10-22', 'period=30', 'period=90', 'sort=mmr&direction=desc', 'per_page=10&page=2'],
            'players': ['', 'account=guest&played=no', 'min_games=1&min_wins=1&max_winrate=90', 'min_deck_count=3',
                'min_mmr=1000&max_mmr=1000', 'sort=last_played&direction=desc',
                'period=custom&date_from=2026-10-22&date_to=2026-10-22', 'per_page=10'],
            'games': ['', 'q=Pirate', 'win_type=combat&win_type=combo', 'timed_mode=chess_clock',
                'date_from=2026-10-22&date_to=2026-10-22', 'min_players=2&max_players=4&min_duration=20',
                f'player_id={self.a.id}&winner_id={self.b.id}&deck_id={self.red.id}', 'scope=all',
                'min_ending_turn=4&sort=duration&direction=asc', 'per_page=10&page=2'],
        }
        for user in (self.user, self.member):
            self.login(user)
            for kind, cases in queries.items():
                for query in cases:
                    with self.subTest(user=user.username, kind=kind, query=query):
                        context, _ = self.read('/' + kind + '?' + query)
                        response = self.client.get('/api/browse/' + kind + '?' + query)
                        data = response.get_json()
                        self.assertEqual(response.status_code, 400 if context['browse'].errors else 200)
                        self.assertEqual([x['id'] for x in data['items']], [x.id for x in context[kind]])
                        self.assertEqual(data['total'], context['pagination'].total)
                        summary = context.get('listing_summary', context.get('games_stats'))
                        # JSON arrays normalize tuple summaries.
                        for key, value in summary.items():
                            if key in data['summary']:
                                self.assertEqual(data['summary'][key], json.loads(json.dumps(value)))
                        for row in data['items']:
                            if kind == 'decks':
                                self.assertEqual(row['uses'], context['deck_stats'][row['id']]['uses'])
                            if kind == 'players':
                                self.assertEqual(row['played'], context['player_stats'][row['id']]['played'])

    def test_invalid_browse_fields_and_authorized_choices(self):
        for query, field in [('min_games=nan', 'min_games'), ('color=C&color=R', 'color'),
                             ('period=custom&date_to=9999-12-31', 'date_to'), ('per_page=7', 'per_page')]:
            response = self.client.get('/api/browse/decks?' + query)
            self.assertEqual(response.status_code, 400)
            self.assertIn(field, response.json['errors'])
            self.assertEqual(response.json['items'], [])
        self.login(self.member)
        response = self.client.get('/api/browse/decks')
        owner = next(f for f in response.json['fields'] if f['name'] == 'player_id')
        self.assertEqual([o[0] for o in owner['options']], ['', str(self.b.id)])
        with self.client.session_transaction() as session: session.clear()
        self.assertEqual(self.client.get('/api/browse/games').status_code, 401)

    def test_statistics_dates_include_last_microsecond_and_reject_invalid(self):
        for route in ('/api/stats', '/api/saltmine'):
            self.assertEqual(self.client.get(route + '?date_to=not-a-date').status_code, 400)
            self.assertEqual(self.client.get(route + '?date_from=2026-10-23&date_to=2026-10-22').status_code, 400)
        response = self.client.get('/api/stats?date_from=2026-10-22&date_to=2026-10-22')
        self.assertEqual(response.status_code, 200)
        self.assertEqual([row['id'] for row in response.json['recent_games']], [self.g1.id])

    def test_profile_password_and_preferences(self):
        self.user.password_hash = generate_password_hash('Old-secret42')
        m.db.session.commit()
        response = self.client.patch('/api/me', json=dict(username='renamed', display_name='Renamed', use_light_theme=True,
            use_sigtaara=True, mana_fucked_salt_value=17, misplayed_salt_value=9))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json['light_theme'])
        self.assertEqual(response.json['mana_fucked_salt_value'], 17)
        self.assertEqual(self.a.name, 'Renamed')
        for value in (51, -1, True, '2', 1.2):
            self.assertEqual(self.client.patch('/api/me', json={'misplayed_salt_value': value}).status_code, 400)
        self.assertEqual(self.client.post('/api/me/password', json={'current_password': 'bad', 'new_password': 'New-secret42'}).status_code, 400)
        self.assertEqual(self.client.post('/api/me/password', json={'current_password': 'Old-secret42', 'new_password': 'New-secret42'}).status_code, 200)
        self.assertTrue(check_password_hash(self.user.password_hash, 'New-secret42'))
        old_version = self.user.session_version
        self.assertEqual(self.client.post('/api/me/sessions/revoke').status_code, 200)
        self.assertEqual(self.user.session_version, old_version + 1)
        self.assertEqual(self.client.get('/api/me').status_code, 200)

    def test_anonymization_and_deck_actions(self):
        self.member.password_hash = generate_password_hash('Secret-password42')
        m.db.session.commit()
        self.login(self.member)
        self.assertEqual(self.client.get(f'/api/decks/{self.blue.id}/export').text, '1 Sol Ring')
        self.assertEqual(self.client.post(f'/api/decks/{self.red.id}/remove-decklist').status_code, 403)
        self.assertEqual(self.client.post(f'/api/decks/{self.blue.id}/strategy-tags', json={'tokens':'no', 'artifacts':'yes'}).status_code, 200)
        self.assertEqual(json.loads(self.blue.tags_json)['_strategy_overrides'], {'tokens':False,'artifacts':True})
        self.assertEqual(self.client.post(f'/api/decks/{self.blue.id}/strategy-tags', json={'tokens':'maybe'}).status_code, 400)
        with patch.object(m, 'compute_deck_tags_from_text', return_value=({'poison':True}, {})):
            self.assertEqual(self.client.post(f'/api/decks/{self.blue.id}/retag').status_code, 200)
        self.assertEqual(self.client.post(f'/api/decks/{self.blue.id}/remove-decklist').status_code, 200)
        self.assertEqual(self.client.post(f'/api/decks/{self.blue.id}/retag').status_code, 400)
        self.assertEqual(self.client.get(f'/api/players/{self.a.id}/profile-picture').status_code, 403)
        self.assertEqual(self.client.get(f'/api/players/{self.b.id}/profile-picture').status_code, 200)
        self.assertEqual(self.client.post('/api/me/anonymize', json={'current_password':'wrong'}).status_code, 400)
        self.assertEqual(self.client.post('/api/me/anonymize', json={'current_password':'Secret-password42'}).status_code, 200)
        self.assertIsNone(self.b.user_id)
        self.assertTrue(self.b.name.startswith('Deleted player'))
        self.assertEqual(self.client.get('/api/me').status_code, 401)

    def test_native_timer_host_authority_and_record_metadata(self):
        timer = game_timers.new_timer({'mode':'chess_clock', 'minutes_per_player':1, 'increment_seconds':2}, [self.a.id,self.b.id], self.a.id, 1000)
        active = m.ActiveGame(created_at=datetime.utcnow(), updated_at=datetime.utcnow(), token='native-clock', host_user_id=self.user.id, pod_id=self.pod.id,
            participants_json=json.dumps([{'player_id':self.a.id}, {'player_id':self.b.id}]),
            state_json=json.dumps({'version':0, 'active_player_id':self.a.id, 'timer':timer}))
        m.db.session.add(active); m.db.session.commit()
        with patch.object(m, 'timer_now_ms', return_value=11000):
            response = self.client.post('/api/game/native-clock/state', json={'player_id':self.a.id,'active_player_id':self.b.id})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json['timer']['remaining_ms'][str(self.a.id)], 52000)
        self.login(self.member)
        with self.client.session_transaction() as session: session['game_join_native-clock'] = self.b.id
        self.assertEqual(self.client.post('/api/game/native-clock/state', json={'player_id':self.b.id,'timer_tick':True}).status_code, 403)
        self.assertEqual(self.client.post('/api/game/native-clock/state', json={'player_id':self.b.id,'pass_turn':True}).status_code, 403)
        self.login(self.user)
        payload = dict(winner_id=self.a.id, participants=[dict(player_id=self.a.id, deck_id=self.red.id, seat_position=1), dict(player_id=self.b.id, deck_id=self.blue.id, seat_position=2)],
            timed_mode='chess_clock', time_control={'minutes_per_player':1,'increment_seconds':2}, ended_on_time=True, timed_out_player_id=self.b.id)
        with patch.object(m, '_apply_mmr_for_game'), patch.object(m, 'get_deck_parsed_tags', return_value={}):
            response = self.client.post('/api/games', json=payload)
        self.assertEqual(response.status_code, 200, response.json)
        self.assertEqual(response.json['timed_mode'], 'chess_clock')
        self.assertEqual(response.json['timed_out_player_id'], self.b.id)


class TimerTransitionTests(unittest.TestCase):
    def test_pauses_background_restore_and_timeout(self):
        timer = game_timers.new_timer({'mode':'turn_timer', 'seconds_per_turn':5}, [1,2], 1, 1000)
        timer = game_timers.tick(timer, 2000)
        timer['paused_reasons'] = ['modal']
        timer = game_timers.tick(timer, 100000)
        self.assertEqual(timer['turn_remaining_ms'], 4000)
        timer['paused_reasons'] = []
        timer = game_timers.advance(timer, 2, 101000)
        self.assertEqual(timer['turn_remaining_ms'], 5000)
        restored = json.loads(json.dumps(timer))
        expired = game_timers.tick(restored, 107000)
        self.assertEqual(expired['timed_out_player_id'], 2)
        self.assertEqual(game_timers.advance(expired, 1, 108000)['active_player_id'], 2)
