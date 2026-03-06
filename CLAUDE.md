# CLAUDE.md

This file provides guidance for AI assistants working on the commander-tracker codebase.

## Project Overview

Commander Tracker is a **Magic: The Gathering Commander (EDH) format** life tracking and deck management web application. It supports multiplayer games (4 players typical), real-time life total tracking, deck import/management, and comprehensive game statistics.

**Tech stack:** Python 3.11, Flask, Flask-SQLAlchemy, SQLite, Jinja2 templates, vanilla JavaScript, Bootstrap 5. Deployed via Docker.

## Repository Structure

```
commander-tracker/
├── app.py                  # Main Flask app: all routes, models, and business logic (~5500 lines)
├── deck_import.py          # Deck parsing module (Moxfield, Archidekt, plaintext, custom sets)
├── requirements.txt        # Python dependencies
├── Dockerfile              # Container build (python:3.11-slim)
├── docker-compose.yaml     # Docker Compose deployment config
├── AGENTS.md               # Test data setup guide for agents
├── data/                   # Runtime data (not in git, mounted as Docker volume)
│   ├── commander.db        # SQLite database
│   └── art/                # Card artwork cache (auto-created)
├── static/                 # Frontend assets (Bootstrap, custom CSS, fonts, icons, MTG SVGs)
│   └── css/                # 14 custom CSS modules
├── templates/              # Jinja2 HTML templates (21 files)
│   ├── base.html           # Layout wrapper
│   ├── life_counter.html   # Real-time game UI (~78KB, main interactive template)
│   └── ...                 # Views for decks, games, players, pods, admin
├── tests/                  # Python unittest test suite
│   ├── test_api_game_state.py
│   ├── test_compute_deck_tags.py
│   └── test_deny_registration_request.py
└── docs/qa/                # QA documentation and viewport testing guides
```

## Running the App

**Local (development):**
```bash
pip install -r requirements.txt
python app.py
```

**Docker:**
```bash
docker-compose up --build
```

The app binds to `0.0.0.0` and uses port 5000 by default. The SQLite database is at `/data/commander.db` (Docker volume: `./data:/data`).

## Running Tests

```bash
python -m pytest tests/
# or individual files:
python -m unittest tests/test_api_game_state.py
python -m unittest tests/test_compute_deck_tags.py
python -m unittest tests/test_deny_registration_request.py
```

Tests use temporary SQLite databases for isolation — no external setup needed. Each test class sets up and tears down its own database schema.

## Environment Variables

| Variable | Required | Description |
|---|---|---|
| `FLASK_SECRET_KEY` | Recommended | Session encryption key |
| `CCAUTO_BASE_URL` | Optional | Custom MTG card gallery endpoint (e.g. `http://custom-mtg-gallery:3000`) |
| `BOOTSTRAP_ADMIN_USERNAME` | Optional | Username to auto-promote to admin on first login |
| `APP_PASSWORD` | Optional | Global app password gate |
| `BOOTSTRAP_TEST_USER` | Dev only | Set to `"1"` to auto-create a test user at startup |
| `AUTO_LOGIN_TEST_USER` | Dev only | Set to `"1"` to auto-authenticate test user |
| `TEST_USERNAME` / `TEST_DISPLAY_NAME` / `TEST_PASSWORD` | Dev only | Test account credentials |
| `TEST_IS_ADMIN` | Dev only | Set to `"1"` to give test user admin rights (default: `"1"`) |

## Database Models

All models are defined in `app.py` and use Flask-SQLAlchemy. The database is SQLite at `/data/commander.db`.

- **User** — Authentication accounts (username, password hash, active/admin flags, preferences)
- **Player** — Game participants (1:1 optional link to User; players can exist without accounts)
- **Deck** — MTG decks with commander info, color identity, decklist text, computed tags
- **Game** — Completed game records (winner, win type, participants, salt rating, timing data)
- **Pod** — Play groups (many-to-many with Player via PodMembership)
- **PodMembership** — Player membership in a pod with role (`member` | `podmaster`)
- **GameParticipant** — Per-player game records (deck played, seat, flags JSON, life delta)
- **RegistrationRequest** — Pending user registrations with approval workflow
- **ActiveGame** — In-progress game sessions (token, state JSON, participants JSON)

**Schema changes:** The app calls `db.create_all()` at startup. Flask-Migrate/Alembic is a dependency but migration scripts are not present in the repo — schema evolution is handled by `db.create_all()` and manual SQL when needed.

## Key Constants and Configuration (app.py top section)

- `DEFAULT_POD_NAME` / `DEFAULT_POD_SLUG` — The hardcoded default pod ("Der Keller – Die Salzmine")
- `DECK_TAGS_VERSION = 2` — Bump this when changing tag computation logic to invalidate cached tags
- `KNOWN_DECK_TAG_KEYS` — Canonical set of deck tag keys: `monarch`, `initiative`, `citys_blessing`, `poison`, `proliferate`, `energy`, `experience`, `mana_fucked`, `misplayed`
- `COMMANDER_BRACKET_FAST_MANA` / `COMMANDER_BRACKET_TUTORS` / `COMMANDER_BRACKET_CEDH_COMBOS` — Card name sets for bracket scoring
- `CANONICAL_WIN_TYPES` — `combat`, `combo`, `alt_win`, `concede`, `time`, `lock`, `other`
- `CANONICAL_TIMED_MODES` — `off`, `chess_clock`, `turn_timer`
- `ALLOWED_PARTICIPANT_FLAG_KEYS` — Valid keys in participant `flags_json`
- `MAX_PARTICIPANT_FLAGS_PAYLOAD_BYTES = 4096` — Size limit on flag payloads
- `MAX_PER_PLAYER_TURN_STATS = 500` — Max turn stats entries per player

## Route Structure

**Authentication:** `/register`, `/login`, `/logout`, `/profile`

**Game flow:**
- `/play_game` — Setup form (select players, decks, pod)
- `/start_game` (POST) — Initialize `ActiveGame`, redirect to life counter
- `/life_counter` — Real-time life counter UI (the main interactive experience)
- `/join/<token>` — Join game via shareable link
- `/api/game/<token>/state` (GET/POST) — Polling API for game state sync
- `/end_game` (POST) — Finalize game, write `Game` record
- `/cancel_game` (POST) — Abandon in-progress game

**Decks:** `/decks`, `/add_deck`, `/deck/<id>`, `/deck/<id>/update`, `/deck/<id>/retire`, `/deck/<id>/plan`, `/deck/<id>/retag`

**Players:** `/players`, `/add_player`, `/player/<id>`, `/delete_player/<id>`

**Pods:** `/pods` (GET/POST), `/pods/<id>/members`, `/pods/<id>/retire`, `/pods/<id>/restore`, `/pods/<id>/delete`

**Admin:** `/admin/users`, `/admin/users/<id>/approve|deny|deactivate|delete|toggle_admin`, `/registration_requests`

**API endpoints:** `/api/game/<token>/state`, `/api/deck-import-preview`, `/api/cards/autocomplete`, `/api/cards/named`, `/api/commander-bracket`, `/api/card-art`, `/api/gallery-image`

**Stats:** `/saltmine` — Leaderboard and game statistics

## Game State API

The life counter uses a polling-based state sync via `/api/game/<token>/state`.

- **GET** — Returns full state JSON
- **POST** — Sends a partial update; server merges and returns new state

State JSON shape:
```json
{
  "life": {"<player_id>": 40},
  "flags": {"<player_id>": {"mana_fucked": false, "misplayed": false, "monarch": false, "poison": 0, "salt_count": 0}},
  "card_state": {
    "<player_id>": {
      "counters": {"shield": 1},
      "commander_damage": {"<source_player_id>": 5},
      "statuses": {"hexproof": true}
    }
  },
  "version": 42,
  "turn": 3,
  "active_player_id": 7
}
```

**Authorization:** Only the host user or a player who has joined via `/join/<token>` (stored in `session[f"game_join_{token}"]`) can POST updates. Players can only update their own `player_id`.

## Deck Tags System

Tags are computed from a deck's `decklist_text` by parsing card oracle text via Scryfall. Tags are cached in `Deck.tags_json` with a version number (`Deck.tags_version`). If `tags_version != DECK_TAGS_VERSION`, tags are recomputed on next access or via `/deck/<id>/retag`.

When changing tag computation logic, **increment `DECK_TAGS_VERSION`** in `app.py`.

## Commander Bracket System

`/api/commander-bracket` accepts a card list and returns a bracket (1–5) based on:
- **+2 points** per fast mana card (Mana Crypt, Jeweled Lotus, etc.)
- **+1 point** per tutor (Demonic Tutor, Vampiric Tutor, etc.)
- **+3 points** per cEDH combo piece (Thassa's Oracle, Demonic Consultation, etc.)

Score → bracket: 0=1, 1–2=2, 3–4=3, 5–7=4, 8+=5

## Deck Import (deck_import.py)

Supports:
- **Moxfield** URLs — fetched via Moxfield API
- **Archidekt** URLs — fetched via Archidekt API
- **Custom gallery (ccauto)** — fetched from `CCAUTO_BASE_URL` if set
- **Plaintext** — one card per line with optional quantity prefix

`parse_deck_input(url_or_text)` is the main entry point. Raises `DeckParserError` on failure.

## Authentication and Authorization

- Session-based auth using Flask sessions; `session["user_id"]` identifies logged-in user
- `@login_required` decorator for protected routes
- `@admin_required` decorator for admin-only routes
- Podmaster role: users with `PodMembership.role == "podmaster"` have elevated permissions for their pod
- Registration requires admin approval (or podmaster approval for pod-specific requests)
- `APP_PASSWORD` environment variable gates access to the entire app if set

## Frontend Conventions

- **No build system** — vanilla JavaScript and CSS only, no npm/webpack
- Bootstrap 5 loaded from local static files (not CDN)
- Custom CSS split into per-feature modules in `static/css/`
- MTG mana symbols served as SVGs from `static/mtg-svg/`
- Custom MTG fonts in `static/fonts/`
- PWA manifest at `static/manifest.webmanifest`
- `life_counter.html` is the most complex template — it contains all real-time game UI logic including responsive layouts for different player counts and viewport sizes

## Testing Conventions

- Tests use Python `unittest` (not pytest fixtures, though pytest can run them)
- Each test class uses `setUpClass` to create a temp SQLite DB, `tearDownClass` to remove it
- `setUp` drops and recreates all tables for test isolation
- Tests use `app.app.test_client()` and `app.app.app_context()` directly — no test framework wrappers
- Test users/players/games are created directly via SQLAlchemy model instances, not via HTTP
- **Do not add `@pytest.fixture` or pytest-style tests** — keep unittest style consistent with existing tests

## Code Conventions

- **All backend in `app.py`** — routes, models, helpers, and business logic coexist in one file. Do not split without strong justification.
- Type annotations use `from __future__ import annotations` for forward-reference support
- `str | None` union syntax (Python 3.10+ style, enabled by `from __future__ import annotations`)
- Flash messages use Bootstrap alert categories: `"success"`, `"danger"`, `"warning"`, `"info"`
- JSON fields (`flags_json`, `tags_json`, `state_json`, `participants_json`) are stored as text and parsed with `json.loads`/`json.dumps`
- Legacy data compatibility: canonicalization helpers (`canonicalize_win_type`, `canonicalize_timed_mode`) normalize old enum values to current canonical forms
- Card names in bracket/tag logic are stored and compared **lowercase**
- Commander damage values are capped at 99; poison counters capped at 10; generic counters capped at 999

## Data Setup for Testing (see also AGENTS.md)

Before testing life-counter or game flows, ensure:
1. At least 4 active players exist
2. Each player has 2+ decks with real decklists
3. Use `https://archidekt.com/decks/10697552/pirates_unmodified` as a canonical import source

The `BOOTSTRAP_TEST_USER` + `AUTO_LOGIN_TEST_USER` env vars allow automated test sessions without manual login.

## Git Workflow

- Feature branches follow the pattern `claude/<description>-<id>` (e.g. `claude/add-pass-turn-button-QiyMr`)
- PRs are merged into `main` using merge commits
- `__pycache__/` and `*.pyc` are gitignored
- `data/` directory (database, art cache) is gitignored
