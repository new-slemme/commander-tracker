"""POST /api/games must be able to persist per-participant life history.

The server already collects [timestamp, life] samples inside
POST /api/game/<token>/state, but only the *web* end_game flow copies them onto the
finished game. A standalone client finishes a game with POST /api/games, which never
touched life history — so every game recorded from the Android app produced an empty
life-over-time chart on the web, even when the table ran through the server-backed
counter and the samples were being collected the whole time.

Two ways in, because there are two kinds of Android game:
  - a server-backed table: pass `game_token` and the server adopts the samples it
    already holds, so the client sends nothing;
  - a local/offline counter: the client passes `life_history` per participant.
"""
import json
import unittest
from datetime import datetime

from werkzeug.security import generate_password_hash

from app import (
    app as flask_app,
    db,
    ActiveGame,
    Deck,
    Game,
    GameParticipant,
    Player,
    Pod,
    PodMembership,
    User,
    MAX_LIFE_HISTORY_SAMPLES,
)


class GameLifeHistoryApiTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        flask_app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
        flask_app.config["TESTING"] = True
        with flask_app.app_context():
            db.create_all()

    @classmethod
    def tearDownClass(cls):
        with flask_app.app_context():
            db.drop_all()

    def setUp(self):
        self.ctx = flask_app.app_context()
        self.ctx.push()
        for model in (GameParticipant, Game, ActiveGame, PodMembership, Deck, Player, User, Pod):
            db.session.query(model).delete()
        db.session.commit()

        self.pod = Pod(name="Test Pod", slug="test-pod", is_active=True)
        db.session.add(self.pod)
        db.session.flush()

        self.user = User(
            username="host",
            display_name="Host",
            password_hash=generate_password_hash("pass"),
            is_active=True,
        )
        db.session.add(self.user)
        db.session.flush()

        self.players = []
        self.decks = []
        for i in range(2):
            player = Player(name=f"P{i + 1}", user_id=self.user.id if i == 0 else None)
            db.session.add(player)
            db.session.flush()
            deck = Deck(name=f"Deck {i + 1}", commander=f"Cmd {i + 1}", player_id=player.id)
            db.session.add(deck)
            db.session.flush()
            db.session.add(PodMembership(pod_id=self.pod.id, player_id=player.id, role="member"))
            self.players.append(player)
            self.decks.append(deck)
        self.user.player = self.players[0]
        db.session.commit()

        self.client = flask_app.test_client()
        with self.client.session_transaction() as sess:
            sess["user_id"] = self.user.id
            sess["session_version"] = self.user.session_version
            sess["active_pod_id"] = self.pod.id

    def tearDown(self):
        db.session.remove()
        self.ctx.pop()

    # ---- helpers ----

    def _payload(self, **overrides):
        payload = {
            "winner_id": self.players[0].id,
            "participants": [
                {"player_id": self.players[0].id, "deck_id": self.decks[0].id, "seat_position": 1},
                {"player_id": self.players[1].id, "deck_id": self.decks[1].id, "seat_position": 2},
            ],
        }
        payload.update(overrides)
        return payload

    def _post(self, payload):
        return self.client.post("/api/games", json=payload)

    def _history_for(self, game_id, player_id):
        row = GameParticipant.query.filter_by(game_id=game_id, player_id=player_id).first()
        return json.loads(row.life_history_json) if row.life_history_json else None

    # ---- client-supplied history ----

    def test_life_history_is_persisted_per_participant(self):
        samples = [[1000, 40], [1010, 37], [1020, 30]]
        payload = self._payload()
        payload["participants"][0]["life_history"] = samples

        resp = self._post(payload)
        self.assertEqual(resp.status_code, 200, resp.data)
        game_id = json.loads(resp.data)["id"]

        self.assertEqual(self._history_for(game_id, self.players[0].id), samples)
        self.assertIsNone(self._history_for(game_id, self.players[1].id))

    def test_life_history_is_returned_by_game_detail(self):
        samples = [[1000, 40], [1010, 37]]
        payload = self._payload()
        payload["participants"][0]["life_history"] = samples
        game_id = json.loads(self._post(payload).data)["id"]

        body = json.loads(self.client.get(f"/api/games/{game_id}").data)
        by_player = {p["player_id"]: p for p in body["participants"]}
        self.assertEqual(by_player[self.players[0].id]["life_history"], samples)
        self.assertEqual(
            by_player[self.players[1].id]["life_history"], [],
            "a participant with no history reports an empty list, not null",
        )

    def test_absent_life_history_is_accepted(self):
        """The field is optional — an older client must still be able to post."""
        resp = self._post(self._payload())
        self.assertEqual(resp.status_code, 200, resp.data)
        game_id = json.loads(resp.data)["id"]
        self.assertIsNone(self._history_for(game_id, self.players[0].id))

    def test_oversized_history_is_capped_not_rejected(self):
        """A long game is not a client error; keep the most recent samples."""
        samples = [[i, 40 - (i % 40)] for i in range(MAX_LIFE_HISTORY_SAMPLES + 50)]
        payload = self._payload()
        payload["participants"][0]["life_history"] = samples

        resp = self._post(payload)
        self.assertEqual(resp.status_code, 200, resp.data)
        stored = self._history_for(json.loads(resp.data)["id"], self.players[0].id)
        self.assertEqual(len(stored), MAX_LIFE_HISTORY_SAMPLES)
        self.assertEqual(stored[-1], samples[-1], "the newest samples are the ones kept")

    def test_single_sample_history_is_not_stored(self):
        """One sample means the life never changed — nothing to chart."""
        payload = self._payload()
        payload["participants"][0]["life_history"] = [[1000, 40]]
        game_id = json.loads(self._post(payload).data)["id"]
        self.assertIsNone(self._history_for(game_id, self.players[0].id))

    # ---- validation ----

    def test_malformed_history_is_rejected(self):
        for bad in (
            "not-a-list",
            [[1000]],
            [[1000, 40], ["ts", 30]],
            [[1000, 40], [1010, "life"]],
            [{"t": 1000, "life": 40}],
            [[1000, 40, 3]],
        ):
            with self.subTest(bad=bad):
                payload = self._payload()
                payload["participants"][0]["life_history"] = bad
                resp = self._post(payload)
                self.assertEqual(
                    resp.status_code, 400,
                    f"expected 400 for {bad!r}, got {resp.status_code}",
                )

    # ---- server-side adoption from an active game ----

    def test_history_is_adopted_from_a_supplied_game_token(self):
        token = "tok-adopt"
        now = datetime.utcnow()
        state = {
            "version": 5,
            "life": {str(self.players[0].id): 12},
            "flags": {},
            "card_state": {},
            "turn": 7,
            "life_history": {
                str(self.players[0].id): [[1000, 40], [1010, 25], [1020, 12]],
                str(self.players[1].id): [[1000, 40], [1015, 0]],
            },
        }
        db.session.add(ActiveGame(
            token=token,
            host_user_id=self.user.id,
            participants_json=json.dumps([]),
            state_json=json.dumps(state),
            created_at=now,
            updated_at=now,
        ))
        db.session.commit()

        payload = self._payload(game_token=token)
        resp = self._post(payload)
        self.assertEqual(resp.status_code, 200, resp.data)
        game_id = json.loads(resp.data)["id"]

        self.assertEqual(
            self._history_for(game_id, self.players[0].id),
            [[1000, 40], [1010, 25], [1020, 12]],
        )
        self.assertEqual(
            self._history_for(game_id, self.players[1].id),
            [[1000, 40], [1015, 0]],
        )

    def test_explicit_history_wins_over_the_token(self):
        """The client's own samples are more specific than the server's snapshot."""
        token = "tok-precedence"
        now = datetime.utcnow()
        db.session.add(ActiveGame(
            token=token,
            host_user_id=self.user.id,
            participants_json=json.dumps([]),
            state_json=json.dumps({
                "life_history": {str(self.players[0].id): [[1, 40], [2, 39]]},
            }),
            created_at=now,
            updated_at=now,
        ))
        db.session.commit()

        explicit = [[10, 40], [20, 5]]
        payload = self._payload(game_token=token)
        payload["participants"][0]["life_history"] = explicit
        game_id = json.loads(self._post(payload).data)["id"]

        self.assertEqual(self._history_for(game_id, self.players[0].id), explicit)

    def test_unknown_token_is_ignored_not_an_error(self):
        resp = self._post(self._payload(game_token="does-not-exist"))
        self.assertEqual(
            resp.status_code, 200,
            "a stale token must not block recording a finished game",
        )

    def test_history_from_a_game_the_caller_was_not_in_is_not_adopted(self):
        """Game tokens are shareable by design — join links hand them out — so
        holding one must not let a caller graft another table's real life history
        onto a game record of their own authoring."""
        stranger = User(
            username="stranger",
            display_name="Stranger",
            password_hash=generate_password_hash("pass"),
            is_active=True,
        )
        db.session.add(stranger)
        db.session.flush()
        stranger_player = Player(name="Stranger", user_id=stranger.id)
        db.session.add(stranger_player)
        db.session.flush()
        stranger.player = stranger_player

        token = "tok-foreign"
        now = datetime.utcnow()
        db.session.add(ActiveGame(
            token=token,
            host_user_id=stranger.id,
            participants_json=json.dumps([
                {"player_id": stranger_player.id, "deck_id": 0, "seat_position": 1},
            ]),
            state_json=json.dumps({
                # Deliberately keyed to *our* players, the way an attacker would
                # need it to be for the graft to land.
                "life_history": {
                    str(self.players[0].id): [[1, 40], [2, 3]],
                    str(self.players[1].id): [[1, 40], [2, 1]],
                },
            }),
            created_at=now,
            updated_at=now,
        ))
        db.session.commit()

        resp = self._post(self._payload(game_token=token))
        self.assertEqual(
            resp.status_code, 200,
            "the game still records; the foreign token is ignored, not an error — "
            "a 403 here would confirm the token exists",
        )
        game_id = json.loads(resp.data)["id"]
        for player in self.players:
            self.assertIsNone(
                self._history_for(game_id, player.id),
                "history from another user's game must not be adopted",
            )

    def test_host_of_the_active_game_may_adopt(self):
        """The common case: the person who started the table records the result."""
        token = "tok-host"
        now = datetime.utcnow()
        db.session.add(ActiveGame(
            token=token,
            host_user_id=self.user.id,
            participants_json=json.dumps([]),
            state_json=json.dumps({
                "life_history": {str(self.players[0].id): [[1, 40], [2, 12]]},
            }),
            created_at=now,
            updated_at=now,
        ))
        db.session.commit()

        game_id = json.loads(self._post(self._payload(game_token=token)).data)["id"]
        self.assertEqual(self._history_for(game_id, self.players[0].id), [[1, 40], [2, 12]])

    def test_a_participant_who_is_not_host_may_adopt(self):
        """Any device at the table can be the one that submits the finished game."""
        other_user = User(
            username="tablemate",
            display_name="Tablemate",
            password_hash=generate_password_hash("pass"),
            is_active=True,
        )
        db.session.add(other_user)
        db.session.flush()

        token = "tok-participant"
        now = datetime.utcnow()
        db.session.add(ActiveGame(
            token=token,
            host_user_id=other_user.id,
            participants_json=json.dumps([
                {"player_id": self.players[0].id, "deck_id": self.decks[0].id, "seat_position": 1},
                {"player_id": self.players[1].id, "deck_id": self.decks[1].id, "seat_position": 2},
            ]),
            state_json=json.dumps({
                "life_history": {str(self.players[0].id): [[1, 40], [2, 9]]},
            }),
            created_at=now,
            updated_at=now,
        ))
        db.session.commit()

        game_id = json.loads(self._post(self._payload(game_token=token)).data)["id"]
        self.assertEqual(
            self._history_for(game_id, self.players[0].id), [[1, 40], [2, 9]],
            "the caller's own player was seated at that table, so adoption is legitimate",
        )

    # ---- integer bounds ----

    def test_out_of_range_integers_are_rejected(self):
        """Python ints are arbitrary-precision, so a value can pass isinstance(int)
        and still be nonsense. Anything past ~4300 digits is refused by json.loads
        before it reaches us; these are the values that *do* parse and serialize
        cleanly and would otherwise be stored and charted as garbage.
        """
        cases = {
            "timestamp far past any real epoch": [[10 ** 20, 40], [1000, 30]],
            "life beyond any real game": [[1000, 40], [1010, 10 ** 20]],
        }
        for label, bad in cases.items():
            with self.subTest(case=label):
                payload = self._payload()
                payload["participants"][0]["life_history"] = bad
                self.assertEqual(self._post(payload).status_code, 400)

    def test_negative_timestamp_is_rejected(self):
        payload = self._payload()
        payload["participants"][0]["life_history"] = [[-5, 40], [10, 30]]
        self.assertEqual(self._post(payload).status_code, 400)

    def test_negative_life_is_allowed(self):
        """Life below zero is legitimate in Commander."""
        samples = [[1000, 40], [1010, -7]]
        payload = self._payload()
        payload["participants"][0]["life_history"] = samples
        game_id = json.loads(self._post(payload).data)["id"]
        self.assertEqual(self._history_for(game_id, self.players[0].id), samples)


if __name__ == "__main__":
    unittest.main()
