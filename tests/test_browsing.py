"""HTML browsing contracts: statistics, filters, visibility, URLs and query scaling."""
import json
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch
from urllib.parse import urlsplit, parse_qs

from flask import template_rendered
from sqlalchemy import event
import app as m
from browsing import listing_return_url


class BrowsingTests(unittest.TestCase):
    def setUp(self):
        m.app.config.update(TESTING=True)
        self.ctx = m.app.app_context()
        self.ctx.push()
        self.addCleanup(self.ctx.pop)
        m.db.session.remove()
        m.db.drop_all()
        m.db.create_all()
        self.user = m.User(username='browse-admin', display_name='Admin', password_hash='unused', is_active=True, is_admin=True)
        self.member = m.User(username='browse-member', display_name='Member', password_hash='unused', is_active=True)
        m.db.session.add_all([self.user, self.member]); m.db.session.flush()
        self.a = m.Player(name='Alice', user_id=self.user.id, historical_games=7, historical_wins=2)
        self.b = m.Player(name='Bob', user_id=self.member.id)
        self.c = m.Player(name='Carol')
        self.secret = m.Player(name='Invisible Player')
        m.db.session.add_all([self.a, self.b, self.c, self.secret]); m.db.session.flush()
        self.pod = m.Pod(name='Visible Pod', slug='browse-visible', owner_user_id=self.user.id)
        self.private = m.Pod(name='Private Pod', slug='browse-private', owner_user_id=self.user.id)
        m.db.session.add_all([self.pod, self.private]); m.db.session.flush()
        for player in [self.a, self.b, self.c]:
            m.db.session.add(m.PodMembership(pod_id=self.pod.id, player_id=player.id))
        m.db.session.add(m.PodMembership(pod_id=self.private.id, player_id=self.secret.id))
        self.red = self.deck('Red Pirates', self.a, 'R', mmr=1200, historical_games=3, historical_wins=1, declared_bracket=3, decklist_text='1 Sol Ring')
        self.blue = self.deck('Blue Artifacts', self.b, 'U', mmr=1000, declared_bracket=2, decklist_text='1 Sol Ring')
        self.multi = self.deck('Multicolor', self.a, 'UR', mmr=800)
        self.empty = self.deck('Colorless', self.c, '')
        self.missing = self.deck('Unknown metadata', self.c, None)
        self.retired = self.deck('Retired', self.a, 'G', retired=True, mmr=2000)
        self.planned = self.deck('Planned', self.a, 'W', planned=True, mmr=2000)
        self.hidden = self.deck('Invisible Deck', self.secret, 'B')
        m.apply_deck_tags(self.red, {'poison': True}, {'strategies': {'tokens': {'cards': ['Sol Ring']}}})
        data = json.loads(self.red.tags_json); data['_analysis']['card_count'] = 100
        self.red.tags_json = json.dumps(data)
        m.apply_deck_tags(self.blue, {'energy': True})
        # Same-day boundaries include the final microsecond; borrowing counts for the actual deck.
        self.g1 = self.game(datetime(2026, 10, 22, 23, 59, 59, 999999), self.b, [(self.b, self.red), (self.a, self.blue)], note='Pirate victory', win_type='combat', timed_mode='chess_clock', duration_seconds=3600, ending_turn=8)
        self.g2 = self.game(datetime(2026, 10, 23), self.a, [(self.a, self.red), (self.b, self.blue)], win_type='combo', duration_seconds=1200, ending_turn=4)
        self.g3 = self.game(datetime(2026, 7, 1), self.a, [(self.a, self.red), (self.b, self.blue)], win_type='other')
        self.game(datetime(2026, 10, 22), self.secret, [(self.secret, self.hidden)], pod=self.private, note='Secret note')
        m.db.session.commit()
        self.client = m.app.test_client()
        self.login(self.user)

    def deck(self, name, owner, colors, **kwargs):
        deck = m.Deck(name=name, commander='Captain ' + name, player_id=owner.id, color_identity=colors, **kwargs)
        m.db.session.add(deck); m.db.session.flush()
        return deck

    def game(self, date, winner, participants, pod=None, **kwargs):
        game = m.Game(date=date, winner_id=winner.id, pod_id=(pod or self.pod).id, **kwargs)
        m.db.session.add(game); m.db.session.flush()
        for player, deck in participants:
            m.db.session.add(m.GameParticipant(game_id=game.id, player_id=player.id, deck_id=deck.id))
        return game

    def login(self, user):
        with self.client.session_transaction() as session:
            session.clear(); session['user_id'] = user.id; session['active_pod_id'] = self.pod.id

    def read(self, path):
        contexts = []
        def capture(sender, template, context, **kwargs): contexts.append(context)
        with template_rendered.connected_to(capture, m.app):
            response = self.client.get(path)
        self.assertEqual(response.status_code, 200, path)
        return contexts[-1], response.get_data(as_text=True)

    def ids(self, path, kind):
        return [r.id for r in self.read(path)[0][kind]]

    def test_deck_filters_individually_and_combined(self):
        cases = [
            ('q=captain+red', [self.red]), ('status=planned', [self.planned]), ('status=retired', [self.retired]),
            ('color=R', [self.multi, self.red]), ('color=R&color=U&color_mode=exact', [self.multi]),
            ('color=R&color_mode=within', [self.empty, self.red]), ('color=C', [self.empty]),
            ('bracket=3', [self.red]), ('bracket=2&bracket=3', [self.blue, self.red]),
            ('strategy=tokens', [self.red]), ('mechanic=energy', [self.blue]), ('freshness=current', [self.red]),
            ('freshness=limited_list', [self.blue]), ('decklist=yes', [self.blue, self.red]),
            ('min_mmr=1200', [self.red]), ('min_games=4', [self.red]), ('min_wins=3', [self.red]),
            ('min_winrate=66.7&max_winrate=66.7', [self.red]),
            ('q=pirate&bracket=3&color=R&strategy=tokens&mechanic=poison&min_games=6&max_mmr=1200', [self.red]),
        ]
        for query, expected in cases:
            with self.subTest(query=query):
                self.assertEqual(self.ids('/decks?' + query, 'decks'), [d.id for d in expected])
        self.assertEqual(len(self.ids('/decks?show_retired=1', 'decks')), 8)
        self.assertEqual(self.ids('/decks?show_retired=1&status=retired', 'decks'), [self.retired.id])
        self.assertNotIn(self.red.id, self.ids('/decks?decklist=no', 'decks'))
        self.assertNotIn(self.missing.id, self.ids('/decks?color=C', 'decks'))
        self.assertIn(self.missing.id, self.ids('/decks?bracket=none', 'decks'))

    def test_statistics_periods_history_borrowing_and_current_mmr(self):
        ctx, _ = self.read('/decks?q=Red')
        self.assertEqual((ctx['deck_stats'][self.red.id]['uses'], ctx['deck_stats'][self.red.id]['wins']), (6, 4))
        # Actual wins: borrower Bob wins once, Alice twice, plus one historical.
        ctx, _ = self.read('/decks?q=Red&period=custom&date_from=2026-10-22&date_to=2026-10-22')
        self.assertEqual((ctx['deck_stats'][self.red.id]['uses'], ctx['deck_stats'][self.red.id]['wins']), (1, 1))
        ctx, _ = self.read('/players?q=Alice&period=custom&date_from=2026-10-22&date_to=2026-10-22')
        stats = ctx['player_stats'][self.a.id]
        self.assertEqual((stats['played'], stats['won'], stats['avg_mmr'], stats['deck_count']), (1, 0, 1000, 4))
        ctx, _ = self.read('/players?q=Alice')
        self.assertEqual((ctx['player_stats'][self.a.id]['played'], ctx['player_stats'][self.a.id]['won']), (10, 4))
        with patch('browsing.datetime', wraps=datetime) as clock:
            clock.utcnow.return_value = datetime(2026, 10, 22, 13)
            for period, start in [('30', datetime(2026, 9, 23)), ('90', datetime(2026, 7, 25))]:
                ctx, _ = self.read('/decks?period=' + period)
                self.assertEqual(ctx['browse'].start, start)
                self.assertEqual(ctx['deck_stats'][self.red.id]['uses'], 1)

    def test_player_filters(self):
        cases = [('q=ali', [self.a]), ('account=guest', [self.c]), ('played=no', [self.c]),
                 ('min_games=5', [self.a]), ('min_wins=2', [self.a]), ('min_winrate=40', [self.a]),
                 ('min_deck_count=4', [self.a]), ('min_mmr=1000&max_mmr=1000', [self.a, self.b, self.c]),
                 ('period=custom&date_from=2026-10-22&date_to=2026-10-22&min_wins=1', [self.b])]
        for query, expected in cases:
            with self.subTest(query=query): self.assertEqual(self.ids('/players?' + query, 'players'), [p.id for p in expected])
        # Carol has active decks: missing MMR is tested with a truly deckless guest.
        no_deck = m.Player(name='No decks'); m.db.session.add(no_deck); m.db.session.flush()
        m.db.session.add(m.PodMembership(pod_id=self.pod.id, player_id=no_deck.id)); m.db.session.commit()
        self.assertNotIn(no_deck.id, self.ids('/players?min_mmr=0', 'players'))
        self.assertEqual(self.ids('/players?sort=mmr&direction=desc', 'players')[-1], no_deck.id)

    def test_game_filters(self):
        cases = [('q=Pirate+victory', [self.g1]), ('q=Captain+Red', [self.g2, self.g1, self.g3]),
            ('date_from=2026-10-22&date_to=2026-10-22', [self.g1]), ('win_type=combat', [self.g1]),
            ('win_type=combat&win_type=combo', [self.g2, self.g1]), ('timed_mode=chess_clock', [self.g1]),
            ('min_duration=40&max_duration=60', [self.g1]), ('min_ending_turn=7', [self.g1]),
            ('min_players=2&max_players=2', [self.g2, self.g1, self.g3]),
            (f'winner_id={self.b.id}&player_id={self.a.id}&deck_id={self.red.id}', [self.g1]),
            ('sort=duration&direction=desc', [self.g1, self.g2, self.g3])]
        for query, expected in cases:
            with self.subTest(query=query): self.assertEqual(self.ids('/games?' + query, 'games'), [g.id for g in expected])
        self.assertNotIn(self.g3.id, self.ids('/games?min_duration=0', 'games'))
        ctx, _ = self.read('/games?win_type=combat')
        self.assertEqual(ctx['games_stats']['total'], 1)

    def test_invalid_filters_fail_closed_and_explain(self):
        for path in ['/decks?min_games=5&max_games=2', '/decks?min_mmr=nan', '/decks?color=garbage',
            '/decks?color=C&color=R', '/decks?status=garbage', '/decks?bracket=99', '/decks?min_games=1.5',
            '/players?period=custom&date_from=2026-10-30&date_to=2026-10-01', '/players?min_winrate=101',
            '/games?date_from=bad', '/games?date_to=9999-12-31', '/games?winner_id=99999',
            '/games?min_duration=inf', '/games?per_page=7', '/games?sort=garbage',
            '/games?sort=', '/games?per_page=', '/decks?period=', '/players?direction=', '/decks?status=',
            '/decks?period=lifetime&date_from=2026-10-01', '/players?period=30&date_to=2026-10-01']:
            with self.subTest(path=path):
                ctx, html = self.read(path)
                self.assertTrue(ctx['browse'].errors)
                self.assertEqual(ctx['pagination'].total, 0)
                self.assertIn('Check the highlighted filters', html)

    def test_visibility_and_no_external_work(self):
        self.login(self.member)
        with patch.object(m, 'compute_commander_bracket_from_text', side_effect=AssertionError('analysis')), patch.object(m, 'compute_deck_tags', side_effect=AssertionError('refresh')):
            self.assertEqual(self.ids('/decks?status=all', 'decks'), [self.blue.id])
            self.assertEqual(self.ids('/decks?player_id=' + str(self.a.id), 'decks'), [])
            for path in ['/decks', '/players', '/games?scope=all']:
                _, html = self.read(path)
                self.assertNotIn('Invisible', html)
            self.assertEqual(self.ids('/games?scope=all&q=Secret', 'games'), [])

    def test_pagination_summary_ties_and_multivalue_urls(self):
        for i in range(30): self.deck('Same name', self.a, 'R', declared_bracket=3)
        m.db.session.commit()
        ctx, _ = self.read('/decks?bracket=2&bracket=3&per_page=10')
        self.assertEqual(ctx['pagination'].total, 32)
        self.assertEqual(ctx['listing_summary']['count'], 32)
        first = [d.id for d in ctx['decks']]
        with m.app.test_request_context('/decks?bracket=2&bracket=3&per_page=10'):
            link = ctx['browse'].url(page=2)
        self.assertEqual(parse_qs(urlsplit(link).query)['bracket'], ['2', '3'])
        second = self.ids(link, 'decks')
        self.assertFalse(set(first) & set(second))
        ctx2, _ = self.read(link)
        self.assertEqual(ctx2['listing_summary'], ctx['listing_summary'])
        self.assertEqual(self.ids(link, 'decks'), second)
        with m.app.test_request_context(link):
            self.assertNotIn('page', parse_qs(urlsplit(ctx2['browse'].chips[0]['url']).query))

    def test_owner_link_before_first_deck_and_all_sort_options(self):
        owner = m.Player(name='New owner')
        m.db.session.add(owner); m.db.session.commit()
        ctx, _ = self.read('/decks?player_id=' + str(owner.id))
        self.assertFalse(ctx['browse'].errors)
        self.assertEqual(ctx['selected_player_id'], owner.id)
        self.assertEqual(ctx['pagination'].total, 0)
        for path, sorts in [('/decks', ['name', 'commander', 'mmr', 'games', 'wins', 'winrate', 'last_played']),
                            ('/players', ['name', 'games', 'wins', 'winrate', 'mmr', 'deck_count', 'last_played']),
                            ('/games', ['date', 'duration', 'ending_turn', 'players'])]:
            for sort in sorts:
                for direction in ['asc', 'desc']:
                    with self.subTest(path=path, sort=sort, direction=direction):
                        ctx, _ = self.read(path + '?sort=' + sort + '&direction=' + direction)
                        self.assertFalse(ctx['browse'].errors)
                        self.assertGreater(ctx['pagination'].total, 0)

    def test_return_url_allowlist(self):
        for value in ['https://example.com/decks', '//example.com/decks', '/decks/../logout', '/logout', '/decks\\evil', '//[broken']:
            self.assertIsNone(listing_return_url(value))
        self.assertEqual(listing_return_url('/decks?bracket=1&bracket=3&page=2'), '/decks?bracket=1&bracket=3&page=2')

    def test_browse_query_count_does_not_scale_with_rows(self):
        def count(path):
            queries = []
            def listen(*args): queries.append(args[2])
            event.listen(m.db.engine, 'before_cursor_execute', listen)
            try: self.read(path)
            finally: event.remove(m.db.engine, 'before_cursor_execute', listen)
            return len(queries)
        paths = ['/decks?per_page=100', '/decks?strategy=tokens', '/players?per_page=100', '/games?per_page=100']
        before = {path: count(path) for path in paths}
        for i in range(30):
            player = m.Player(name=f'Bulk {i}'); m.db.session.add(player); m.db.session.flush()
            m.db.session.add(m.PodMembership(pod_id=self.pod.id, player_id=player.id))
            deck = self.deck(f'Bulk deck {i}', player, 'G')
            self.game(datetime(2026, 10, 20), player, [(player, deck), (self.a, self.red)])
        m.db.session.commit(); m.db.session.expire_all()
        for path in paths:
            with self.subTest(path=path): self.assertLessEqual(count(path), before[path] + 1)
