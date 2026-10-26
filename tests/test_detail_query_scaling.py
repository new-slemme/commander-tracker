"""Detail reads must retain their data without issuing a query for every row."""
from datetime import datetime, timedelta
import unittest

from flask import template_rendered
from sqlalchemy import event

import app


class DetailQueryScalingTests(unittest.TestCase):
    def setUp(self):
        app.app.config.update(TESTING=True)
        with app.app.app_context():
            app.db.session.remove()
            app.db.drop_all()
            app.db.create_all()
            user = app.User(username='performance-admin', display_name='Performance Admin',
                            password_hash='unused', is_admin=True, is_active=True)
            app.db.session.add(user)
            app.db.session.flush()
            player = app.Player(name='Performance Player', user_id=user.id,
                                historical_wins=2, historical_games=5)
            peer = app.Player(name='Performance Peer')
            app.db.session.add_all([player, peer])
            app.db.session.flush()
            deck = app.Deck(name='Performance Deck', commander='', player_id=player.id,
                            historical_wins=1, historical_games=3)
            app.db.session.add(deck)
            app.db.session.commit()
            self.uid, self.pid, self.peer_id, self.did = user.id, player.id, peer.id, deck.id
            self.engine = app.db.engine
        self.client = app.app.test_client()
        with self.client.session_transaction() as session:
            session['user_id'] = self.uid
        self.add_games(0, 3)

    def add_games(self, start, stop):
        with app.app.app_context():
            for index in range(start, stop):
                opponent = app.Player(name=f'Opponent {index}')
                app.db.session.add(opponent)
                app.db.session.flush()
                opposing_deck = app.Deck(name=f'Opponent deck {index}', commander='', player_id=opponent.id)
                # More owned decks exercise aggregates across both owners and borrowers.
                borrowed_deck = app.Deck(name=f'Borrowed deck {index}', commander='', player_id=self.pid)
                app.db.session.add_all([opposing_deck, borrowed_deck])
                app.db.session.flush()
                game = app.Game(date=datetime(2026, 1, 1) + timedelta(days=index),
                                winner_id=self.pid if index % 2 == 0 else opponent.id,
                                win_type='combat', starting_player_id=self.pid)
                app.db.session.add(game)
                app.db.session.flush()
                app.db.session.add_all([
                    app.GameParticipant(game_id=game.id, player_id=self.pid, deck_id=self.did),
                    app.GameParticipant(game_id=game.id, player_id=opponent.id, deck_id=opposing_deck.id),
                    app.GameParticipant(game_id=game.id, player_id=self.peer_id, deck_id=borrowed_deck.id),
                ])
            app.db.session.commit()

    def read(self, path):
        statements = []
        contexts = []

        def query(conn, cursor, statement, parameters, context, executemany):
            statements.append(statement)

        def rendered(sender, template, context, **extra):
            contexts.append(context)

        event.listen(self.engine, 'before_cursor_execute', query)
        try:
            with template_rendered.connected_to(rendered, app.app):
                response = self.client.get(path)
        finally:
            event.remove(self.engine, 'before_cursor_execute', query)
        self.assertEqual(response.status_code, 200, path)
        return len(statements), response, contexts[-1] if contexts else None

    def test_queries_stay_bounded_as_history_and_deck_count_grow(self):
        paths = [f'/player/{self.pid}', f'/api/players/{self.pid}',
                 f'/deck/{self.did}', f'/api/decks/{self.did}',
                 f'/player/{self.pid}/export', f'/api/players/{self.pid}/export', '/api/decks']
        before = {path: self.read(path)[0] for path in paths}
        self.add_games(3, 30)
        for path in paths:
            with self.subTest(path=path):
                count, _, _ = self.read(path)
                self.assertLessEqual(count, before[path] + 1)
                self.assertLess(count, 20)

    def test_history_counts_matchups_and_recovered_totals_are_preserved(self):
        self.add_games(3, 30)
        _, response, _ = self.read(f'/api/players/{self.pid}')
        data = response.json
        self.assertEqual((data['games_won'], data['games_played']), (17, 35))
        deck = next(d for d in data['decks'] if d['id'] == self.did)
        self.assertEqual((deck['wins'], deck['games']), (16, 33))
        self.assertEqual(len(data['recent_games']), 10)
        self.assertTrue(all(g['participant_count'] == 3 for g in data['recent_games']))
        dates = [g['date'] for g in data['recent_games']]
        self.assertEqual(dates, sorted(dates, reverse=True))

        _, _, player_page = self.read(f'/player/{self.pid}')
        self.assertEqual(len(player_page['recent_games']), 30)
        self.assertTrue(all(g['participant_count'] == 3 for g in player_page['recent_games']))
        self.assertEqual(player_page['deck_stats'][self.did]['wins'], 16)

        _, response, _ = self.read(f'/api/decks/{self.did}')
        self.assertEqual(len(response.json['recent_games']), 20)
        self.assertTrue(all(g['participant_count'] == 3 for g in response.json['recent_games']))
        _, _, deck_page = self.read(f'/deck/{self.did}')
        self.assertEqual(len(deck_page['history']), 30)
        self.assertTrue(all(len(g['opponents']) == 2 for g in deck_page['history']))
        self.assertEqual(deck_page['matchups']['Performance Peer'],
                         {'wins': 15, 'losses': 15, 'games': 30, 'winrate': 50.0})
        for path in (f'/player/{self.pid}/export', f'/api/players/{self.pid}/export'):
            _, response, _ = self.read(path)
            self.assertEqual(len(response.json['games']), 30)
            self.assertTrue(all(g['participant_count'] == 3 for g in response.json['games']))

    def test_borrowed_deck_win_belongs_to_pilot_and_empty_decks_remain_zero(self):
        with app.app.app_context():
            borrowed = app.Deck(name='Borrowed winning deck', commander='', player_id=self.pid)
            empty = app.Deck(name='Never played', commander='', player_id=self.pid)
            app.db.session.add_all([borrowed, empty])
            app.db.session.flush()
            game = app.Game(winner_id=self.peer_id)
            app.db.session.add(game)
            app.db.session.flush()
            app.db.session.add(app.GameParticipant(game_id=game.id, player_id=self.peer_id, deck_id=borrowed.id))
            app.db.session.commit()
            borrowed_id, empty_id = borrowed.id, empty.id
        _, response, _ = self.read(f'/api/players/{self.pid}')
        decks = {d['id']: d for d in response.json['decks']}
        self.assertEqual((decks[borrowed_id]['wins'], decks[borrowed_id]['games']), (1, 1))
        self.assertEqual((decks[empty_id]['wins'], decks[empty_id]['games']), (0, 0))


if __name__ == '__main__':
    unittest.main()
