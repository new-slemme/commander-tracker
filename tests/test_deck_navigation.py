"""Deck detail round trips preserve the overview's explicit filter context."""
import re
import unittest
from html import unescape
from urllib.parse import parse_qs, urlsplit
from unittest.mock import PropertyMock, patch

from app import app, db, User, Player, Deck


class DeckNavigationTests(unittest.TestCase):
    def setUp(self):
        app.config.update(TESTING=True)
        with app.app_context():
            db.session.remove()
            db.drop_all()
            db.create_all()
            user = User(username="navigation-admin", display_name="Admin",
                        password_hash="unused", is_active=True, is_admin=True)
            db.session.add(user)
            db.session.flush()
            owner = Player(name="Navigation Owner", user_id=user.id)
            other = Player(name="Other Owner")
            db.session.add_all([owner, other])
            db.session.flush()
            deck = Deck(name="Navigation Deck", commander="", player_id=owner.id)
            retired = Deck(name="Retired Deck", commander="", player_id=owner.id, retired=True)
            other_deck = Deck(name="Other Deck", commander="", player_id=other.id)
            db.session.add_all([deck, retired, other_deck])
            db.session.commit()
            self.owner_id, self.deck_id = owner.id, deck.id
            self.retired_id, self.other_id = retired.id, other_deck.id
            user_id = user.id
        self.client = app.test_client()
        with self.client.session_transaction() as session:
            session['user_id'] = user_id
        art = patch.object(Deck, 'commander_art_url', new_callable=PropertyMock, return_value=None)
        art.start()
        self.addCleanup(art.stop)

    def get_html(self, url):
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        return response.get_data(as_text=True)

    def back_url(self, detail_url):
        html = self.get_html(detail_url)
        match = re.search(r'href="([^"]+)"\s*>\s*← Go Back', html)
        self.assertIsNotNone(match)
        return unescape(match.group(1))

    def test_filtered_overview_detail_and_back_round_trip(self):
        for retired in (False, True):
            with self.subTest(show_retired=retired):
                query = f'player_id={self.owner_id}' + ('&show_retired=1' if retired else '')
                overview = self.get_html('/decks?' + query)
                detail_url = unescape(re.search(r'class="deck-tile[^\"]*"\s+href="([^"]+)"', overview).group(1))
                self.assertEqual(parse_qs(urlsplit(detail_url).query), parse_qs(query))
                back_url = self.back_url(detail_url)
                self.assertEqual(parse_qs(urlsplit(back_url).query), parse_qs(query))
                returned = self.get_html(back_url)
                self.assertIn(f'data-deck-id="{self.deck_id}"', returned)
                self.assertNotIn(f'data-deck-id="{self.other_id}"', returned)
                self.assertEqual(f'data-deck-id="{self.retired_id}"' in returned, retired)
                self.assertRegex(returned, rf'<option value="{self.owner_id}"\s+selected>')

    def test_direct_detail_link_returns_to_unfiltered_overview(self):
        self.assertEqual(self.back_url(f'/deck/{self.deck_id}'), '/decks')

    def test_invalid_context_does_not_become_a_return_destination(self):
        self.assertEqual(self.back_url(f'/deck/{self.deck_id}?player_id=https://example.com&show_retired=invalid'), '/decks')

    def test_clearing_filter_does_not_restore_old_context(self):
        self.get_html(f'/decks?player_id={self.owner_id}')
        html = self.get_html('/decks')
        detail_url = unescape(re.search(r'class="deck-tile[^\"]*"\s+href="([^"]+)"', html).group(1))
        self.assertEqual(urlsplit(detail_url).query, '')
        self.assertEqual(self.back_url(detail_url), '/decks')
