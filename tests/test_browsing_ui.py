"""Navigation permissions, current location, and persistent filter visibility."""
from html.parser import HTMLParser
import unittest

from app import app, db, User, Player


class Elements(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.elements = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        self.elements.append((tag, dict(attrs)))

    def by_id(self, element_id):
        return next(attrs for _, attrs in self.elements if attrs.get('id') == element_id)


class BrowsingUITests(unittest.TestCase):
    def setUp(self):
        app.config['TESTING'] = True
        self.ctx = app.app_context()
        self.ctx.push()
        self.addCleanup(self.ctx.pop)
        db.drop_all()
        db.create_all()
        user = User(username='ui-member', display_name='UI Member', password_hash='unused', is_active=True, is_admin=False)
        db.session.add(user)
        db.session.flush()
        db.session.add(Player(name='UI Member', user_id=user.id))
        db.session.commit()
        self.user = user
        self.client = app.test_client()
        with self.client.session_transaction() as session:
            session['user_id'] = user.id

    def page(self, path):
        response = self.client.get(path)
        self.assertEqual(response.status_code, 200)
        return Elements(response.get_data(as_text=True))

    def test_current_location_in_desktop_and_mobile_navigation(self):
        for path in ['/', '/players', '/decks', '/games']:
            with self.subTest(path=path):
                page = self.page(path)
                current = [a for tag, a in page.elements if tag == 'a' and a.get('aria-current') == 'page']
                self.assertEqual([a['href'] for a in current], [path, path])

    def test_account_menu_preserves_admin_permissions(self):
        for admin in [False, True]:
            self.user.is_admin = admin
            db.session.commit()
            with self.client.session_transaction() as session:
                session['is_admin'] = admin
            page = self.page('/players')
            links = [attrs.get('href') for tag, attrs in page.elements if tag == 'a']
            self.assertEqual('/admin/users' in links, admin)
            self.assertIn('/profile', links)
            self.assertIn('/logout', links)

    def test_collapsed_filters_preserve_applied_values_and_show_chips(self):
        for path, field, value in [('/games?min_players=3', 'min_players', '3'),
                                   ('/games?date_from=2026-01-01', 'date_from', '2026-01-01'),
                                   ('/decks?min_games=2', 'min_games', '2'),
                                   ('/players?min_wins=1', 'min_wins', '1')]:
            with self.subTest(path=path):
                page = self.page(path)
                self.assertNotIn('open', page.by_id('browseFilters'))
                self.assertEqual(page.by_id('browse-' + field)['value'], value)
                self.assertTrue(any('browse-chip' in attrs.get('class', '').split()
                                    for _, attrs in page.elements))

    def test_all_listings_begin_with_collapsed_filters(self):
        for path in ['/games', '/decks', '/players']:
            with self.subTest(path=path):
                page = self.page(path)
                self.assertNotIn('open', page.by_id('browseFilters'))

    def test_invalid_filters_open_for_correction(self):
        for path in ['/games?min_players=bad', '/decks?min_games=-1', '/players?min_winrate=101']:
            with self.subTest(path=path):
                self.assertIn('open', self.page(path).by_id('browseFilters'))

    def test_closed_mobile_drawer_is_not_keyboard_focusable(self):
        self.assertIn('inert', self.page('/players').by_id('menuDrawer'))
