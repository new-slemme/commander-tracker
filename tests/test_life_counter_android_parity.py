import unittest
from pathlib import Path

import app


class LifeCounterAndroidParityTests(unittest.TestCase):
    def setUp(self):
        with app.app.app_context():
            app.db.session.remove()
            app.db.drop_all()
            app.db.create_all()

            self.host = app.User(
                username="life-host",
                display_name="Life Host",
                password_hash="x",
                is_active=True,
            )
            self.player_one = app.Player(name="Player One")
            self.player_two = app.Player(name="Player Two")
            app.db.session.add_all([self.host, self.player_one, self.player_two])
            app.db.session.flush()

            self.deck_one = app.Deck(name="Deck One", commander="Commander One", player_id=self.player_one.id)
            self.deck_two = app.Deck(name="Deck Two", commander="Commander Two", player_id=self.player_two.id)
            app.db.session.add_all([self.deck_one, self.deck_two])
            app.db.session.flush()
            self.host_id = self.host.id
            self.player_one_id = self.player_one.id
            self.player_two_id = self.player_two.id
            self.deck_one_id = self.deck_one.id
            self.deck_two_id = self.deck_two.id
            app.db.session.commit()

    def test_life_counter_renders_android_style_deck_identity_and_life_meter(self):
        client = app.app.test_client()
        with client.session_transaction() as sess:
            sess["user_id"] = self.host_id
            sess["game_participants"] = [
                {
                    "player_id": self.player_one_id,
                    "deck_id": self.deck_one_id,
                    "player_name": "Player One",
                    "deck_name": "Deck One",
                    "seat_position": 1,
                },
                {
                    "player_id": self.player_two_id,
                    "deck_id": self.deck_two_id,
                    "player_name": "Player Two",
                    "deck_name": "Deck Two",
                    "seat_position": 2,
                },
            ]
            sess["active_player_id"] = self.player_one_id
            sess["timer_config"] = {"mode": "off"}
            sess["turn_number"] = 1

        response = client.get("/life_counter")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Deck One", response.data)
        self.assertIn(b'class="life-meter"', response.data)

    def test_mobile_menu_structure_uses_compact_root_and_secondary_panels(self):
        client = app.app.test_client()
        with client.session_transaction() as sess:
            sess["user_id"] = self.host_id
            sess["game_participants"] = [
                {
                    "player_id": self.player_one_id,
                    "deck_id": self.deck_one_id,
                    "player_name": "Player One",
                    "deck_name": "Deck One",
                    "seat_position": 1,
                },
                {
                    "player_id": self.player_two_id,
                    "deck_id": self.deck_two_id,
                    "player_name": "Player Two",
                    "deck_name": "Deck Two",
                    "seat_position": 2,
                },
            ]
            sess["active_player_id"] = self.player_one_id
            sess["timer_config"] = {"mode": "off"}
            sess["turn_number"] = 1

        response = client.get("/life_counter")
        html = response.get_data(as_text=True)
        css = Path(app.__file__).resolve().parent.joinpath("static/css/life_counter.css").read_text()

        self.assertEqual(response.status_code, 200)
        self.assertIn('data-game-menu-panel="root"', html)
        self.assertIn('data-game-menu-panel="display"', html)
        self.assertIn('data-game-menu-panel="leave"', html)
        self.assertIn('data-player-salt-panel="root"', html)
        self.assertIn('data-player-salt-panel="flags"', html)
        self.assertIn('data-player-salt-panel="game-state"', html)
        self.assertIn('id="playerSaltAddBtn"', html)
        self.assertIn('function showGameMenuPanel(', html)
        self.assertIn('function showPlayerSaltPanel(', html)
        self.assertIn('function getPlayerSaltMenuEdge(', html)
        self.assertIn('function getPlayerSaltMenuRotation(', html)
        self.assertIn('function dockPlayerSaltMenuToPlayer(', html)
        self.assertIn('flyoutEl.dataset.edge = getPlayerSaltMenuEdge(playerCard)', html)
        self.assertIn('flyoutEl.style.setProperty("--player-menu-rotation", getPlayerSaltMenuRotation(playerCard))', html)
        self.assertIn('class="player-salt-flyout__surface"', html)
        self.assertNotIn('positionPlayerSaltMenu(', html)
        self.assertNotIn('--flyout-rotation', html)
        self.assertIn('.player-salt-flyout.is-mobile-sheet', css)
        self.assertIn('rotate(var(--player-menu-rotation))', css)
        self.assertIn('.player-salt-flyout__surface', css)
        self.assertIn('.player-salt-flyout[data-edge="top"]', css)
        self.assertIn('.player-salt-flyout[data-edge="bottom"]', css)
        self.assertIn('.player-salt-flyout[data-edge="left"]', css)
        self.assertIn('.player-salt-flyout[data-edge="right"]', css)
        self.assertIn('100dvh', css)


if __name__ == "__main__":
    unittest.main()
