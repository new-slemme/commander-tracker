"""Recent-player shortcuts must follow game dates within the setup pod."""
from datetime import datetime
from html.parser import HTMLParser
import unittest

from app import app, db, User, Player, Deck, Game, GameParticipant, Pod, PodMembership


class PickerButtons(HTMLParser):
    def __init__(self):
        super().__init__()
        self.quick = []
        self.named = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag != 'button' or 'data-player-id' not in attrs:
            return
        classes = attrs.get('class', '').split()
        if 'player-choice--quick' in classes:
            self.quick.append(int(attrs['data-player-id']))
        if 'player-choice--named' in classes:
            self.named.append(int(attrs['data-player-id']))


class RecentPlayerPickerTests(unittest.TestCase):
    def setUp(self):
        app.config['TESTING'] = True
        self.context = app.app_context()
        self.context.push()
        self.addCleanup(self.context.pop)
        db.drop_all()
        db.create_all()
        self.user = User(username='picker-admin', display_name='Picker Admin', password_hash='unused',
                         is_active=True, is_admin=True)
        self.pod = Pod(name='Picker Pod', slug='picker-pod', is_active=True)
        self.other = Pod(name='Other Pod', slug='other-pod', is_active=True)
        db.session.add_all([self.user, self.pod, self.other])
        db.session.flush()
        self.players = []
        self.decks = {}
        for name in ['Alpha', 'Bravo', 'Charlie', 'Delta', 'Echo', 'Foxtrot']:
            player = Player(name=name)
            db.session.add(player)
            db.session.flush()
            db.session.add(PodMembership(player_id=player.id, pod_id=self.pod.id, role='member'))
            deck = Deck(name=name+' deck', commander='Admiral Beckett Brass', player_id=player.id)
            db.session.add(deck)
            db.session.flush()
            self.players.append(player)
            self.decks[player.id] = deck
        self.players[0].user_id = self.user.id
        db.session.commit()
        self.client = app.test_client()
        with self.client.session_transaction() as session:
            session['user_id'] = self.user.id
            session['active_pod_id'] = self.pod.id
            session['nav_scope'] = 'all'  # Setup still belongs to its active pod.

    def game(self, date, players, pod=None):
        game = Game(date=date, winner_id=players[0].id, pod_id=(pod or self.pod).id, win_type='combat')
        db.session.add(game)
        db.session.flush()
        for seat, player in enumerate(players, 1):
            db.session.add(GameParticipant(game_id=game.id, player_id=player.id,
                                          deck_id=self.decks[player.id].id, seat_position=seat))
        db.session.commit()

    def buttons(self):
        response = self.client.get('/play_game')
        self.assertEqual(response.status_code, 200)
        parser = PickerButtons()
        parser.feed(response.get_data(as_text=True))
        return parser.quick[:len(self.players)], parser.named

    def test_recent_participation_uses_latest_date_and_alphabetical_ties(self):
        a, b, c, d, e, f = self.players
        self.game(datetime(2026, 9, 20), [e, d])
        self.game(datetime(2026, 8, 1), [c])
        self.game(datetime(2026, 7, 1), [b])
        self.game(datetime(2020, 1, 1), [a])
        # A higher game ID with an old date must not displace recent play.
        self.game(datetime(1999, 1, 1), [e])
        quick, named = self.buttons()
        self.assertEqual(quick, [p.id for p in [d, e, c, b, a, f]])
        self.assertEqual(named, [p.id for p in self.players])

    def test_other_pod_games_do_not_change_shortcuts_or_add_players(self):
        a, b, c, d, e, f = self.players
        self.game(datetime(2026, 9, 1), [d, e])
        self.game(datetime(2027, 1, 1), [a], pod=self.other)
        outsider = Player(name='Outside Player')
        db.session.add(outsider)
        db.session.flush()
        db.session.add(PodMembership(player_id=outsider.id, pod_id=self.other.id, role='member'))
        db.session.commit()
        quick, named = self.buttons()
        self.assertEqual(quick, [p.id for p in [d, e, a, b, c, f]])
        self.assertNotIn(outsider.id, named)

    def test_without_games_all_players_remain_available_alphabetically(self):
        quick, named = self.buttons()
        expected = [p.id for p in self.players]
        self.assertEqual(quick, expected)
        self.assertEqual(named, expected)
