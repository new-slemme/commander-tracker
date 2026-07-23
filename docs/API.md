# Commander Tracker API (Standalone Client Guide)

This document describes the HTTP API exposed by Commander Tracker for standalone clients (for example Android/iOS apps).

> Current implementation reference: `app.py` (Flask app). Responses are JSON unless noted otherwise.

> **Historical stat baseline:** `wins`, `played`/`uses`, and `winrate` on player and deck
> objects include a recovered pre-wipe baseline (games lost in an April data loss that
> survive only as aggregate tallies). These fields therefore reflect true all-time totals,
> not just rows currently in the DB. The baseline is attributed to the default pod, so it is
> included in unscoped/default-pod and lifetime views but omitted from other-pod or
> date-filtered stats. Distinct game **counts** (e.g. `total_games`) count real game rows plus
> the recovered games; per-player `played` is a participation count and can exceed
> `total_games`. Seat/starting-player and matchup breakdowns are not baseline-adjusted.

## 1) Base URL and transport

- **Base URL (local/dev):** `http://localhost:5000`
- **API prefix:** `/api`
- **Auth model:** session cookie (Flask session), not bearer token.

A standalone client must preserve and resend cookies after login.

---

## 2) Authentication and session behavior

## Login

`POST /api/login`

Request body:

```json
{
  "username": "alice",
  "password": "secret"
}
```

Success `200`:

```json
{
  "user_id": 1,
  "username": "alice",
  "display_name": "Alice",
  "is_admin": false,
  "player_id": 3
}
```

Error responses:
- `400` invalid/missing JSON body
- `401` invalid credentials
- `403` account exists but is not active/approved

## Logout

`POST /api/logout`

Success `200`:

```json
{ "message": "Logged out" }
```

## Current user

`GET /api/me`

Requires authenticated session.

Success `200`:

```json
{
  "user_id": 1,
  "username": "alice",
  "display_name": "Alice",
  "is_admin": false,
  "player_id": 3
}
```

Error `401` if not authenticated.

## Authorization summary

- Most `/api/*` routes require an authenticated session.
- Notable public API routes:
  - `POST /api/login`
  - `POST /api/logout`
  - `GET /api/card-art`
  - `GET /api/gallery-image`
  - `GET /api/cards/autocomplete`
  - `GET /api/cards/named`
  - `POST /api/commander-bracket`
  - `GET|POST /api/game/<token>/state` (with additional join/host checks for POST)
  - `GET|POST /api/join/<token>`

When unauthorized, API routes generally return:

```json
{ "error": "Unauthorized" }
```

with status `401`.

---

## 3) Core REST resources (players, decks, games, stats)

## Stats

`GET /api/stats` (auth required)

Returns aggregated player/deck stats plus recent games for the active scope/pod.

Top-level shape:

```json
{
  "player_stats": [],
  "recent_games": [],
  "top_decks": [],
  "scope": "global_or_pod_scope",
  "pod_name": "Optional Pod Name"
}
```

## Players

### List players

`GET /api/players` (auth required)

Response `200`:

```json
[
  {
    "id": 1,
    "name": "Player 1",
    "wins": 12,
    "played": 34,
    "winrate": 35.3,
    "deck_count": 5
  }
]
```

### Player detail

`GET /api/players/<player_id>` (auth required)

Response includes:
- player summary (`games_played`, `games_won`, `winrate`)
- `decks[]`
- `recent_games[]` (up to 20)

Error `404` if player does not exist.

## Games

### List games

`GET /api/games` (auth required)

Query params:
- `player_id` (int, optional)
- `deck_id` (int, optional)
- `winner_id` (int, optional)
- `page` (int, default `1`)
- `per_page` (int, default `25`, max `100`)

Response `200`:

```json
{
  "games": [
    {
      "id": 101,
      "date": "2026-01-01T12:34:56",
      "winner": { "id": 1, "name": "Player 1" },
      "win_type": "combat",
      "ending_turn": 9,
      "participants": []
    }
  ],
  "page": 1,
  "pages": 5,
  "total": 117,
  "per_page": 25
}
```

### Game detail

`GET /api/games/<game_id>` (auth required)

Includes participant-level fields like `seat_position`, `salt_count`, `mana_fucked`, `misplayed`.

Error `404` if game does not exist.

## Decks

### List decks

`GET /api/decks` (auth required)

Query params:
- `player_id` (admin only filter)
- `show_retired=1` to include retired/planned decks

Response items include:
- identity: `id`, `name`, `commander`, `player_id`, `player_name`
- state: `retired`, `planned`
- performance: `wins`, `uses`, `winrate`
- visual/meta: `art_url`, `mechanics`

### Deck detail

`GET /api/decks/<deck_id>` (auth required)

Adds `recent_games[]` (up to 20). Error `404` if missing.

### Create deck

`POST /api/decks` (auth required)

Creates a new deck and returns deck summary data.

Request body example (non-admin creating for own player):

```json
{
  "name": "Pirate Party",
  "commander": "Admiral Beckett Brass",
  "raw_import": "1 Admiral Beckett Brass\n1 Sol Ring\n1 Island"
}
```

Request body example (admin setting explicit owner):

```json
{
  "player_id": 12,
  "name": "Pirate Party",
  "commander": "Admiral Beckett Brass",
  "decklist_text": "1 Admiral Beckett Brass\n1 Sol Ring\n1 Island",
  "retired": false,
  "planned": false
}
```

Success `201`:

```json
{
  "id": 42,
  "name": "Pirate Party",
  "commander": "Admiral Beckett Brass",
  "retired": false,
  "planned": false,
  "player_id": 12,
  "player_name": "Player 12",
  "wins": 0,
  "uses": 0,
  "winrate": 0.0,
  "art_url": null,
  "mechanics": {
    "monarch": false,
    "poison": false,
    "energy": false,
    "experience": false
  }
}
```

Errors:
- `400` invalid JSON body, missing required fields (`name`, owner context), invalid import payload type, or deck parsing/validation errors
- `401` not authenticated
- `403` non-admin attempted to set `player_id` to another player

### Update deck

`PATCH /api/decks/<deck_id>` or `PUT /api/decks/<deck_id>` (auth required)

Updates an existing deck. `PATCH` supports partial updates; `PUT` requires `name`.

Request body example (`PATCH`):

```json
{
  "name": "Pirate Party v2",
  "commander": "Admiral Beckett Brass",
  "raw_import": "1 Admiral Beckett Brass\n1 Sol Ring\n1 Arcane Signet"
}
```

Request body example (`PUT`):

```json
{
  "name": "Pirate Party",
  "commander": "Admiral Beckett Brass",
  "decklist_text": "1 Admiral Beckett Brass\n1 Sol Ring\n1 Island",
  "retired": false,
  "planned": false
}
```

Success `200`:

```json
{
  "id": 42,
  "name": "Pirate Party v2",
  "commander": "Admiral Beckett Brass",
  "retired": false,
  "planned": false,
  "player_id": 12,
  "player_name": "Player 12",
  "wins": 3,
  "uses": 7,
  "winrate": 42.9,
  "art_url": null,
  "mechanics": {
    "monarch": false,
    "poison": false,
    "energy": false,
    "experience": false
  },
  "recent_games": []
}
```

Errors:
- `400` invalid JSON body or invalid field values (for example: empty `name` on `PATCH`, missing `name` on `PUT`, invalid `raw_import` type)
- `401` not authenticated
- `403` attempting to update a deck you do not own (non-admin)
- `404` deck not found


---

## 4) Live game APIs (join + state sync)

These endpoints power multiplayer/life-counter sync.

## Seat-claim / join API

### Get join payload

`GET /api/join/<token>`

Success `200`:

```json
{
  "token": "abc123",
  "participants": [
    { "player_id": 1, "player_name": "Player 1", "deck_id": 10 }
  ]
}
```

Error `404` if game token not found.

### Claim a seat

`POST /api/join/<token>`

Request body:

```json
{ "player_id": 1 }
```

On success, server stores `session["game_join_<token>"] = player_id` and returns:

```json
{
  "token": "abc123",
  "player_id": 1,
  "participants": [],
  "state": {}
}
```

Errors:
- `400` invalid body / invalid player selection
- `404` game token not found

## Game state endpoint

`GET|POST /api/game/<token>/state`

### GET
Returns the entire state JSON for the active game token.

### POST
Accepts a **partial update**, merges into existing state, increments `version`, persists, and returns full updated state.

Required body field:
- `player_id` (int)

Authorization for POST:
- **Host user** (creator) can update any player.
- Non-host must have claimed a seat via `/api/join/<token>` and can only update their claimed `player_id`.

Common request fields:

```json
{
  "player_id": 1,
  "life": 37,
  "life_delta": -1,
  "flags": {
    "mana_fucked": false,
    "misplayed": false,
    "salt_count": 2
  },
  "card_state": {
    "counters": { "shield": 1 },
    "commander_damage": { "2": 5 },
    "statuses": { "hexproof": true }
  },
  "active_player_id": 2,
  "turn": 5,
  "pass_turn": true
}
```

State fields returned by server:

```json
{
  "life": { "1": 40 },
  "flags": {
    "1": {
      "mana_fucked": false,
      "misplayed": false,
      "salt_count": 0
    }
  },
  "card_state": {
    "1": {
      "counters": { "shield": 1 },
      "commander_damage": { "2": 5 },
      "statuses": { "hexproof": true }
    }
  },
  "version": 42,
  "turn": 3,
  "active_player_id": 2,
  "passed": [1]
}
```

Server-side validation/highlights:
- `life_delta` must be between `-1000` and `1000`
- resulting life is clamped at minimum `0`
- `salt_count` must be non-negative integer
- `commander_damage` source IDs must be valid participants; values are sanitized/capped
- only host can set `active_player_id` and `turn`
- `pass_turn` advances to next participant and increments turn after full cycle

Typical errors:
- `404` game token not found
- `400` invalid payload (`player_id`, `life`, etc.)
- `403` player not in game / seat-claim authorization failure

---

## 5) Card and deck utility endpoints

## Card art lookup

`GET /api/card-art?name=<card name>`

Returns:

```json
{ "image": "https://..." }
```

If missing name, returns `{ "image": null }`.

## Gallery image proxy

`GET /api/gallery-image?path=/api/sets/...`

Returns raw image bytes with content-type if proxy succeeds, otherwise `404` text response (`Not found`).

## Card autocomplete

`GET /api/cards/autocomplete?q=<prefix>`

Returns merged/deduplicated suggestions:

```json
{ "data": ["Sol Ring", "Solemn Simulacrum"] }
```

If query is empty: `{ "data": [] }`.

## Exact card lookup

`GET /api/cards/named?exact=<card name>`

- Tries Scryfall first, then optional custom gallery backend.
- Returns card JSON object on success.
- `400` when `exact` is missing.
- `404` when card is not found.

## Commander bracket

`POST /api/commander-bracket`

Accepts either:
- `{"cards": ["Card A", "Card B"]}`
- `{"decklist_text": "1 Sol Ring\n1 Arcane Signet\n..."}`

Returns computed bracket payload (implementation-defined keys from bracket calculator).

Error `400` if neither `cards` nor `decklist_text` is provided.

## Deck import preview

`POST /api/deck-import-preview` (authenticated web session required)

Request:

```json
{ "raw_import": "https://archidekt.com/decks/..." }
```

Success:

```json
{
  "commander": "Captain N'ghathrod",
  "commanders": ["Captain N'ghathrod"],
  "primary_commander": "Captain N'ghathrod",
  "partner_commander": null
}
```

If `raw_import` is empty:

```json
{ "commander": null, "commanders": [] }
```

Validation errors return `400` with `{"error": "..."}`.

---

## 6) Recommended standalone-client flow

1. `POST /api/login` and persist session cookies.
2. `GET /api/me` to identify user and linked `player_id`.
3. Load data views as needed:
   - `GET /api/players`
   - `GET /api/decks`
   - `GET /api/games`
4. For live game companion mode:
   - `GET /api/join/<token>`
   - `POST /api/join/<token>` to claim seat
   - Poll `GET /api/game/<token>/state`
   - Send interactions via `POST /api/game/<token>/state`

---

## 7) Notes for Android/iOS implementation

- Use a cookie-capable HTTP client (`OkHttp` cookie jar, URLSession HTTPCookieStorage, etc.).
- Treat `401` as session-expired/unauthenticated; trigger re-login.
- Use short polling for live state (the server is polling-based, not websocket-based).
- Be resilient to extra fields: state payload can evolve over time.
