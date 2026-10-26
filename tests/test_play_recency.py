"""Play picker API/web parity. Uses the isolated test-suite database."""
import unittest
from datetime import datetime
from tests import test_browsing as fixtures
import app as m


class PlayRecencyTests(unittest.TestCase):
    setUp = fixtures.BrowsingTests.setUp
    deck = fixtures.BrowsingTests.deck
    game = fixtures.BrowsingTests.game
    login = fixtures.BrowsingTests.login
    read = fixtures.BrowsingTests.read

    def test_recency_is_pod_scoped_and_matches_web(self):
        self.game(datetime(2026, 10, 25), self.b, [(self.b, self.blue)])
        self.game(datetime(2026, 10, 26), self.a, [(self.a, self.red)], pod=self.private)
        m.db.session.commit()
        response = self.client.get('/api/players')
        self.assertEqual(response.status_code, 200)
        rows = response.json
        by_id = {p['id']: p for p in rows}
        self.assertEqual(by_id[self.a.id]['last_played_at'], '2026-10-23T00:00:00')
        self.assertEqual(by_id[self.b.id]['last_played_at'], '2026-10-25T00:00:00')
        self.assertIsNone(by_id[self.c.id]['last_played_at'])
        self.assertNotIn(self.secret.id, by_id)
        context, _ = self.read('/play_game')
        ordered = sorted(rows, key=lambda p: (p['name'], p['id']))
        ordered.sort(key=lambda p: p['last_played_at'] or '', reverse=True)
        self.assertEqual([p['id'] for p in ordered], [p.id for p in context['quick_players']])

    def test_ties_keep_alphabetical_order_and_empty_input(self):
        rows = self.client.get('/api/players').json
        self.assertEqual(rows[0]['last_played_at'], rows[1]['last_played_at'])
        context, _ = self.read('/play_game')
        self.assertEqual([p.id for p in context['quick_players']], [self.a.id, self.b.id, self.c.id])
        self.assertEqual(m.player_last_played([], self.pod), {})
