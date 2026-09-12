# Commander Tracker API (Standalone Client Guide)

This document describes the HTTP API exposed by Commander Tracker for standalone clients
(for example the EDH Son Android app at `~/mine/edh-son-android`).

> **This document is enforced.** `tests/test_api_contract.py` compares the endpoint index in
> section 4 against `app.url_map` and fails the build on drift, in either direction. It also
> verifies the documented auth level against what the server actually returns to an
> unauthenticated caller. If you add, remove, or re-scope an `/api` route, update section 4 in
> the same commit or the suite goes red.

> Current implementation reference: `app.py` (Flask app). Responses are JSON unless noted otherwise.

> **Historical stat baseline:** `wins`, `played`/`uses`, and `winrate` on player and deck
> objects include a recovered pre-wipe baseline (games lost in an April data loss that
> survive only as aggregate tallies). These fields therefore reflect true all-time totals,
> not just rows currently in the DB. The baseline is attributed to the default pod, so it is
> included in unscoped/default-pod and lifetime views but omitted from other-pod or
> date-filtered stats. Distinct game **counts** (e.g. `total_games`) count real game rows plus
> the recovered games; per-player `played` is a participation count and can exceed
> `total_games`. Seat/starting-player and matchup breakdowns are not baseline-adjusted.

---

## 1) Base URL and transport

- **Base URL (local/dev):** `http://localhost:5000`
- **Base URL (production):** `https://edh.figurensohn.de`
- **API prefix:** `/api`
- **Auth model:** session cookie (Flask session), **not** a bearer token. There is no API-key or
  token auth path anywhere in the application.

A standalone client must preserve and resend cookies after login.

---

## 2) Authentication and session behaviour

### Cookie and session

| Property | Value |
|---|---|
| Cookie name | `session` (Flask default) |
| `HttpOnly` | yes |
| `SameSite` | `Lax` |
| `Secure` | set when `APP_ENV=production` |
| Lifetime | 30 days |

### CSRF

`/api/*` routes are **exempt** from CSRF checks. The custom CSRF gate in `require_login()` applies
only to state-changing requests on non-API routes; `SameSite=Lax` is what mitigates CSRF for the
API surface. A client therefore does **not** need to send `X-CSRFToken` for `/api` calls.

Non-API routes (the two legacy deck routes in section 5.7, for instance) **do** require
`X-CSRFToken` or a `_csrf_token` form field matching the session value.

### Unauthorized

Any `/api` request without a valid session for a non-public endpoint returns:

```
401 {"error": "Unauthorized"}
```

### Session revocation

`User.session_version` is compared against the session on every request. It is incremented when an
administrator edits another user's identity, and on password reset. When it no longer matches — or
the account has been deleted — the server clears the session and the request itself returns
`401 {"error": "Unauthorized"}`.

Revocation is answered with JSON on `/api`, never with a redirect to the login page. A client that
follows redirects would otherwise receive `200` with an HTML login page and misreport a revoked
session as a malformed server URL.

A client must treat `401` as "this session is finished": clear the stored cookie, stop background
work, and re-authenticate. A `401` is **not** interchangeable with a `5xx`; a server fault must not
sign the user out.

---

## 3) Conventions and error handling

### Status codes

| Code | Meaning for a client |
|---|---|
| `400` | Malformed or missing body/params. Do not retry unchanged. |
| `401` | No valid session. Re-authenticate. |
| `403` | Authenticated but not permitted. |
| `404` | Resource absent, or token not found. |
| `409` | Refused because of state (e.g. deleting a deck that has recorded games). |
| `429` | Rate limited. Back off. |
| `503` | Transient. **Retry with backoff** — see below. |

### `503` on live game state — clients must handle this

`POST /api/game/{token}/state` serialises concurrent writers with a dedicated AUTOCOMMIT engine and
`BEGIN IMMEDIATE` so that two devices at the same table cannot lose each other's updates. When the
write lock is contended the request returns:

```
503 {"error": "State is busy, please retry"}
```

This is expected under normal multi-device play and is **not** an error condition to surface to the
user. A client must retry with exponential backoff.

Note that `life_delta` is a *relative* adjustment and therefore **not idempotent** — a blind retry
can double-apply the change. Either retry against the observed `version` and reconcile, or use the
absolute `life` field, which is idempotent.

### Rate limits

| Endpoint | Limit |
|---|---|
| `POST /api/login` | 10 / minute |
| `POST /api/register` | 5 / hour |
| `POST /api/password/forgot` | 5 / hour |
| `POST /api/password/reset` | 10 / hour |
| `POST /api/email/verify` | 10 / hour |
| `POST /api/email/verify/resend` | 5 / hour |
| `POST /api/pods/{pod_id}/invites` | 20 / hour |
| `POST /api/invite/{token}` | 10 / hour |
| `POST /api/games/{game_id}/shares` | 20 / hour |

Limits are per client IP and shared with the equivalent web form, so a client behind a NAT that
also has browser users can see `429` sooner than it expects. Surface it as "too many attempts,
try again later" rather than as a failure of the credentials themselves.

---

## 4) Endpoint index

Complete and machine-checked. `auth` values:

- **public** — reachable with no session
- **auth** — any signed-in user
- **admin** — signed-in administrator (checked inline in the handler)
- **token** — authorised by the game token in the path rather than by session

| Method | Path | Auth |
|---|---|---|
| `GET` | `/api/capabilities` | public |
| `POST` | `/api/login` | public |
| `POST` | `/api/logout` | public |
| `GET` | `/api/me` | auth |
| `POST` | `/api/register` | public |
| `POST` | `/api/password/forgot` | public |
| `POST` | `/api/password/reset` | public |
| `POST` | `/api/email/verify` | public |
| `POST` | `/api/email/verify/resend` | auth |
| `GET` | `/api/homepage` | public |
| `GET` | `/api/mobile/releases/android/latest` | auth |
| `GET` | `/api/stats` | auth |
| `GET` | `/api/saltmine` | auth |
| `GET` | `/api/compare` | auth |
| `GET` | `/api/search` | auth |
| `GET` | `/api/players` | auth |
| `POST` | `/api/players` | auth |
| `GET` | `/api/players/{player_id}` | auth |
| `PATCH` | `/api/players/{player_id}` | admin |
| `DELETE` | `/api/players/{player_id}` | admin |
| `GET` | `/api/players/{player_id}/export` | auth |
| `GET` | `/api/decks` | auth |
| `POST` | `/api/decks` | auth |
| `GET` | `/api/decks/{deck_id}` | auth |
| `PATCH` | `/api/decks/{deck_id}` | auth |
| `PUT` | `/api/decks/{deck_id}` | auth |
| `DELETE` | `/api/decks/{deck_id}` | auth |
| `POST` | `/api/decks/{deck_id}/upload-art` | auth |
| `GET` | `/api/games` | auth |
| `POST` | `/api/games` | auth |
| `GET` | `/api/games/{game_id}` | auth |
| `DELETE` | `/api/games/{game_id}` | admin |
| `GET` | `/api/pods` | auth |
| `POST` | `/api/pods` | auth |
| `GET` | `/api/pods/{pod_id}` | auth |
| `PATCH` | `/api/pods/{pod_id}` | auth |
| `DELETE` | `/api/pods/{pod_id}` | admin |
| `POST` | `/api/pods/{pod_id}/switch` | auth |
| `GET` | `/api/pods/{pod_id}/invites` | auth |
| `POST` | `/api/pods/{pod_id}/invites` | auth |
| `POST` | `/api/pods/{pod_id}/invites/{invite_id}/revoke` | auth |
| `GET` | `/api/invite/{token}` | public |
| `POST` | `/api/invite/{token}` | auth |
| `POST` | `/api/pods/{pod_id}/guests` | auth |
| `GET` | `/api/games/{game_id}/shares` | auth |
| `POST` | `/api/games/{game_id}/shares` | auth |
| `POST` | `/api/games/{game_id}/shares/{share_id}/revoke` | auth |
| `GET` | `/api/recap/{token}` | public |
| `POST` | `/api/pods/{pod_id}/retire` | admin |
| `POST` | `/api/pods/{pod_id}/restore` | admin |
| `POST` | `/api/pods/{pod_id}/members` | auth |
| `PATCH` | `/api/pods/{pod_id}/members/{player_id}` | auth |
| `DELETE` | `/api/pods/{pod_id}/members/{player_id}` | auth |
| `GET` | `/api/admin/users` | admin |
| `PATCH` | `/api/admin/users/{user_id}` | admin |
| `DELETE` | `/api/admin/users/{user_id}` | admin |
| `POST` | `/api/admin/users/{user_id}/approve` | admin |
| `POST` | `/api/admin/users/{user_id}/deny` | admin |
| `POST` | `/api/admin/users/{user_id}/deactivate` | admin |
| `POST` | `/api/admin/users/{user_id}/toggle-admin` | admin |
| `GET` | `/api/registration-requests` | auth |
| `POST` | `/api/registration-requests/{request_id}/approve` | auth |
| `POST` | `/api/registration-requests/{request_id}/deny` | auth |
| `POST` | `/api/start_game` | auth |
| `GET` | `/api/game/{token}/state` | token |
| `POST` | `/api/game/{token}/state` | token |
| `GET` | `/api/join/{token}` | token |
| `POST` | `/api/join/{token}` | token |
| `GET` | `/api/card-art` | public |
| `GET` | `/api/gallery-image` | public |
| `GET` | `/api/cards/autocomplete` | public |
| `GET` | `/api/cards/named` | public |
| `GET` | `/api/cards/prints` | auth |
| `GET` | `/api/ccauto/sets` | auth |
| `GET` | `/api/ccauto/sets/{set_name}` | auth |
| `POST` | `/api/commander-bracket` | public |
| `POST` | `/api/deck-import-parse` | auth |
| `POST` | `/api/deck-import-preview` | auth |

> **Known inconsistency.** `/api/cards/prints` and `/api/ccauto/*` require a session while their
> sibling card lookups (`/api/card-art`, `/api/cards/autocomplete`, `/api/cards/named`) do not.
> The table above records what the server *does*. Whether the card/gallery endpoints should be
> uniformly public or uniformly authenticated is an open product decision — changing it widens or
> narrows access to the custom gallery data and should be decided deliberately, not by drift.

---

## 5) Endpoint reference

### 5.1 Session

#### `GET /api/capabilities` · public

Feature discovery. Call it before login to decide which flows to offer, and again after signing in
for the full set.

**The response depends on whether you are signed in.** Anonymous callers get only the flags that
gate pre-auth UI:

```json
{
  "contract_version": 1,
  "features": {
    "registration": true,
    "password_reset": true,
    "email_verification": true
  }
}
```

Signed in, the full set:

```json
{
  "contract_version": 1,
  "features": {
    "registration": true, "password_reset": true, "email_verification": true,
    "search": true, "compare": true, "mmr": true,
    "life_history": true, "pod_invites": true, "guest_players": true,
    "game_shares": true, "pod_config": false, "account_export": false
  }
}
```

The remaining flags are only actionable with a session, so they are not published to anonymous
callers. A client that needs them should re-read this endpoint after login.

A `404` here means a server older than contract version 1 — assume the pre-2.0 baseline and hide
everything the flags would otherwise gate. Treat an absent key the same as `false`, but note that
absent-while-anonymous means "ask again once signed in", not "unsupported".

#### `POST /api/login` · public · 10/min

```json
{ "username": "alice", "password": "secret" }
```

`200`:

```json
{
  "user_id": 1,
  "username": "alice",
  "display_name": "Alice",
  "is_admin": false,
  "player_id": 3,
  "can_access_registration_requests": false
}
```

`400` malformed body · `401` invalid credentials · `429` rate limited.

#### `POST /api/logout` · public

`200 {"message": "Logged out"}`. Safe to call without a session.

#### `GET /api/me` · auth

The login response plus the account state a client needs in order to render itself:

```json
{
  "user_id": 1,
  "username": "alice",
  "display_name": "Alice",
  "is_admin": false,
  "player_id": 3,
  "can_access_registration_requests": false,
  "email": "alice@example.com",
  "email_verified": false,
  "session_version": 0,
  "owned_pod_ids": [4],
  "use_sigtaara": false
}
```

Use it to validate a restored cookie at startup. `email_verified` is what gates the
verification banner; `owned_pod_ids` tells you which pods this account may administer;
`session_version` is the counter described under [Session revocation](#session-revocation) —
store it alongside the cookie so a later mismatch is recognisable as a revocation.

`email` is `null` for accounts created before email was collected, and for anonymized ones.

---

### 5.1a Onboarding

Four public endpoints and one authenticated one, so a client can take a person from install to
playing without opening a browser. All of them are rate limited; see [Rate limits](#rate-limits).

Validation failures carry a `field` naming what to highlight:

```json
{ "error": "Passwords do not match", "field": "confirm" }
```

`field` is one of `username`, `display_name`, `email`, `pod_name`, `password`, `confirm`. It is
absent on errors that are not about a particular input.

Length limits, enforced before the uniqueness checks: `username` and `display_name` and
`pod_name` 100 characters, `email` 320, `password` 1024. Exceeding one returns `400` naming
that field.

#### `POST /api/register` · public · 5/hour

Creates the account, its player, and the pod it owns, with the new account as podmaster.

```json
{
  "username": "alice",
  "display_name": "Alice",
  "email": "alice@example.com",
  "pod_name": "Friday Crew",
  "password": "correct horse 9",
  "confirm": "correct horse 9"
}
```

`confirm` is optional — omit it if the client already compared the two fields itself. When present
it must match.

`201`:

```json
{
  "user_id": 7,
  "username": "alice",
  "display_name": "Alice",
  "player_id": 12,
  "email": "alice@example.com",
  "email_verified": false,
  "verification_email_sent": true,
  "pod": { "id": 4, "name": "Friday Crew", "slug": "friday-crew" }
}
```

`verification_email_sent` is `false` when the account was created but the mail relay would
not take the message. The account and its session are real either way — the request is **not**
a failure — so treat it as "created, verification pending" and offer Resend rather than telling
the user to check an inbox nothing was sent to.

**The response sets the session cookie** — unlike the web form, which redirects to a login page, a
successful registration signs the client in. Do not follow it with `POST /api/login`.

The address is not verified yet. A verification email goes out immediately; see
`POST /api/email/verify` below.

`400` invalid input (with `field`) · `409` username or email already in use (with `field`) ·
`429` rate limited.

Passwords must be at least 8 characters, contain a letter and a digit, and not appear in the
server's list of common weak passwords. The rejection message is safe to show verbatim.

#### `POST /api/password/forgot` · public · 5/hour

```json
{ "email": "alice@example.com" }
```

`202` — always, and always with the same body:

```json
{ "message": "If that email is registered, a reset link has been sent." }
```

Whether the address is registered is deliberately not disclosed. A client must not infer anything
from timing or status either; show the message as given.

#### `POST /api/password/reset` · public · 10/hour

```json
{ "token": "<from the emailed link>", "password": "new correct horse 9" }
```

The token travels in the body rather than the path so it stays out of access logs and referrer
headers. Extract it from the `/reset-password/<token>` URL in the email.

`200 {"message": "Password updated. Sign in with your new password."}`

**Completing a reset revokes every session on every device**, including any the client still holds.
Send the user back to sign-in afterwards rather than assuming the current cookie survived.

`400` — either the token is unknown or expired (one hour), or the new password fails the rules. The
two are distinguishable only by the presence of `field: "password"`; an unknown token and an expired
one are answered identically on purpose. A rejected password does **not** consume the token, so the
user can correct it and retry with the same link.

#### `POST /api/email/verify` · public · 10/hour

```json
{ "token": "<from the emailed link>" }
```

`200 {"email_verified": true}` · `400` unknown or expired token (24 hours).

Single use: the token is burned on success, so a second call with it returns `400`.

This is a `POST` where the web route is a `GET`, because a mail client or link scanner that
prefetches an emailed URL must not be able to spend a one-shot token. Extract the token from the
`/verify-email/<token>` URL — an Android client can capture it with an App Link on that path and
then call this endpoint.

#### `POST /api/email/verify/resend` · auth · 5/hour

No body. Mints a new verification token, **invalidating any previous one**, and mails it.

`202 {"email_verified": false, "verification_email_sent": true, "message": "Verification email sent. Check your inbox."}`

As on registration, `verification_email_sent: false` (still `202`) means the relay refused the
message; the token was issued and the previous one retired, so a later retry works.

`200 {"email_verified": true, "message": "That address is already verified."}` when there is
nothing to do — treat it as success and dismiss the banner rather than as an error.

`400` when the account has no email address on file · `401` no session.

---

### 5.2 Client updates

#### `GET /api/mobile/releases/android/latest` · auth

Serves `apk/android-latest.json`. **Requires a session** — an unauthenticated call returns `401`,
so a client must not check for updates before login.

```json
{
  "applicationId": "de.slemme.edhcompanion",
  "versionCode": 59,
  "versionName": "1.9.0",
  "minSupportedVersionCode": 6,
  "releaseChannel": "stable",
  "artifactFileName": "edh-companion-1.9.0+59.apk",
  "artifactPath": "/apk/edh-companion-1.9.0+59.apk",
  "artifactUrl": "https://edh.figurensohn.de/apk/edh-companion-1.9.0+59.apk",
  "sha256": "…",
  "publishedAt": "2026-07-03T00:00:00Z",
  "notes": ""
}
```

The APK binary itself (`GET /apk/{filename}`) is served **without authentication**.

---

### 5.3 Stats and analytics

#### `GET /api/stats` · auth

Query: `date_from`, `date_to` (`YYYY-MM-DD`). Scope follows the session's active pod.

```json
{
  "player_stats": [{"player_id": 3, "name": "Alice", "wins": 12, "played": 30, "winrate": 40.0}],
  "recent_games": [],
  "top_decks": [],
  "scope": "pod",
  "pod_name": "Kitchen Table",
  "date_from": null,
  "date_to": null
}
```

`recent_games` is capped at 10, `top_decks` at 6.

#### `GET /api/saltmine` · auth

Query: `date_from`, `date_to`.

```json
{
  "scope": "pod",
  "pod_name": "Kitchen Table",
  "starting_player": {"games": 40, "wins": 12, "winrate": 30.0, "seat_winrates": []},
  "salty_players": [],
  "salty_decks": [],
  "salty_games": [],
  "mechanic_stats": [
    {"mechanic": "monarch", "uses": 9, "wins": 4, "winrate": 44.4,
     "games_with_capability": 20, "activated_games": 9, "activation_rate": 45.0}
  ],
  "date_from": null,
  "date_to": null
}
```

Each leaderboard is capped at 10 entries.

#### `GET /api/compare` · auth

Query: `a`, `b` — player ids.

```json
{
  "player_a": {"id": 3, "name": "Alice"},
  "player_b": {"id": 4, "name": "Bob"},
  "stats_a": {"played": 30, "won": 12, "winrate": 40.0, "deck_count": 5},
  "stats_b": {"played": 28, "won": 9, "winrate": 32.1, "deck_count": 4},
  "h2h_a_wins": 6,
  "h2h_b_wins": 4,
  "h2h_other": 2,
  "shared_games": []
}
```

#### `GET /api/search` · auth

Query: `q`. Pod-scoped, each group bounded.

```json
{ "players": [], "decks": [], "actions": [] }
```

#### `GET /api/homepage` · public

Public summary for the marketing site. Sends
`Access-Control-Allow-Origin: https://figurensohn.de`.

```json
{ "total_games": 42, "players": [{"name": "Alice", "wins": 12, "played": 30, "winrate": 40.0}],
  "last_game": {} }
```

---

### 5.4 Players

#### `GET /api/players` · auth

Pod-scoped list.

```json
[{"id": 3, "name": "Alice", "wins": 12, "played": 30, "winrate": 40.0, "deck_count": 5}]
```

#### `POST /api/players` · auth

`{"name": "Carol"}` → `201` with the summary shape above, zeroed.

#### `GET /api/players/{player_id}` · auth

```json
{
  "id": 3, "name": "Alice",
  "games_played": 30, "games_won": 12, "winrate": 40.0,
  "decks": [], "recent_games": [],
  "accent": "var(--accent-3)",
  "full_page_url": "/player/3"
}
```

`recent_games` capped at 10. `accent` and `full_page_url` exist for the web entity drawer; clients
may ignore them.

#### `PATCH /api/players/{player_id}` · admin

`{"name": "New Name"}` → `{"id": 3, "name": "New Name"}`.

#### `DELETE /api/players/{player_id}` · admin

`{"ok": true}`. Returns `409` if the player has recorded games, history, or decks with history.

#### `GET /api/players/{player_id}/export` · auth

Self or admin. Full JSON export — account link, stats, decks with decklists and tags, and every
game with per-participant salt, mana-fucked, misplayed, and life-delta values.

---

### 5.5 Decks

#### `GET /api/decks` · auth

Query: `player_id`, `show_retired`.

```json
[{
  "id": 7, "name": "Atraxa Superfriends", "commander": "Atraxa, Praetors' Voice",
  "retired": false, "planned": false,
  "player_id": 3, "player_name": "Alice",
  "wins": 5, "uses": 14, "winrate": 35.7,
  "art_url": "/art/atraxa.jpg",
  "mechanics": {"monarch": false, "initiative": false, "citys_blessing": false,
                "poison": true, "energy": false, "experience": false},
  "mmr": 1042,
  "mmr_tier": "C"
}]
```

**`mmr` and `mmr_tier` are always present.** Decks start at `1000`. Tiers: `S` ≥ 1300, `A` ≥ 1200,
`B` ≥ 1100, `C` ≥ 950, otherwise `D`.

#### `GET /api/decks/{deck_id}` · auth

Summary fields plus `recent_games` (≤20), `decklist_text`, `card_print_prefs`,
`custom_commander_art_url`, `owner_accent`, `full_page_url`.

#### `POST /api/decks` · auth

```json
{ "name": "New Deck", "commander": "Kenrith", "planned": false, "raw_import": "1 Sol Ring\n…" }
```

#### `PATCH` / `PUT /api/decks/{deck_id}` · auth, owner or admin

Any of `name`, `commander`, `decklist_text`, `raw_import`, `planned`, `retired`,
`card_print_prefs`, `custom_commander_art_url`. Retire/unretire and plan/unplan are just this
endpoint with the single relevant boolean.

#### `DELETE /api/decks/{deck_id}` · auth, owner or admin

`409` if the deck has recorded games or history.

#### `POST /api/decks/{deck_id}/upload-art` · auth, owner or admin

`multipart/form-data`: `field` (`commander` or `card`) and `file`. Returns `{"url": "…"}`.

---

#### Deck MMR

Every deck summary carries `mmr` (an integer rating, floor applied) and `mmr_tier`, one of
`S` `A` `B` `C` `D` with the same boundaries the web uses:

| Tier | Rating |
|---|---|
| `S` | ≥ 1300 |
| `A` | 1200–1299 |
| `B` | 1100–1199 |
| `C` | 950–1099 |
| `D` | < 950 |

`GET /api/decks/{deck_id}` adds `mmr_history`, the rating after each of the deck's recent games,
oldest first:

```json
"mmr_history": [
  { "mmr": 1010, "delta": 10, "game_id": 41 },
  { "mmr": 1002, "delta": -8, "game_id": 44 }
]
```

Intended for a sparkline, so it is plotted by game order rather than by date and carries no
timestamp. Capped at the **60 most recent** entries — a deck gains one per game forever, and the
list endpoint omits the field entirely to keep many tiles cheap. An empty array means the deck has
not played yet. Unparseable or incomplete stored entries are skipped rather than failing the
request, so treat a shorter array than expected as normal.

---

### 5.6 Games

#### `GET /api/games` · auth

Query: `page`, `per_page` (≤100), `player_id`, `deck_id`, `winner_id`, `date_from`, `date_to`.

```json
{ "games": [], "page": 1, "pages": 3, "total": 42, "per_page": 25 }
```

#### `POST /api/games` · auth

Records an already-finished game. This is how a standalone client submits a game it ran itself.

```json
{
  "winner_id": 3,
  "starting_player_id": 4,
  "win_type": "combat",
  "ending_turn": 11,
  "note": "close one",
  "date": "2026-08-30",
  "game_token": "abc123",
  "participants": [
    {"player_id": 3, "deck_id": 7, "seat_position": 1, "borrow": false,
     "salt_count": 0, "mana_fucked": false, "misplayed": false,
     "commander_damage": {"4": 21},
     "life_history": [[1756512000, 40], [1756512120, 33]]}
  ]
}
```

2–6 participants, seats 1–6, no duplicate players or seats. Deck ownership is validated unless
`borrow` is set. MMR is applied on creation. Returns the created game in the
`GET /api/games/{game_id}` shape.

**Life history** (capability `life_history`). A finished game can carry per-player life-over-time
samples, which the web renders as a chart on the game page. Two ways to supply them:

- **Server-backed table** — pass the top-level `game_token` from `POST /api/start_game`. The server
  already accumulated samples under that token and adopts them, so the client sends nothing. An
  unknown or expired token is ignored rather than refused, so a stale token never blocks recording.
- **Local or offline counter** — pass `life_history` per participant as `[[unix_timestamp, life], …]`.

Explicit `life_history` wins over anything adopted from the token. Both are optional; an older
client that sends neither still posts normally. Fewer than two samples is treated as no history
(the life never changed). More than 120 samples per player is capped to the most recent, not
rejected — but a list longer than 2400 samples is refused with a `400`, since a game that long
is not plausible and validating a list that size is work a client should not be able to demand.
Malformed samples are a `400`.

#### `GET /api/games/{game_id}` · auth

Each entry in `participants` carries the seat's own story, matching what the web game page shows:

| Field | Meaning |
|---|---|
| `monarch` | the seat took the monarch at some point |
| `poison` | poison counters dealt, clamped to 10 |
| `mmr_delta` | rating change from this game, or `null` when the game was never rated |
| `mechanics` | the deck's capabilities (`monarch`, `initiative`, `citys_blessing`, `poison`, `energy`, `experience`) |
| `salt_count`, `mana_fucked`, `misplayed` | the salt flags |
| `life_history` | `[[unix_timestamp, life], …]`, or `[]` |

`mmr_delta` is `null` rather than `0` when no rating was applied — "played and gained nothing" is a
different fact from "was never rated", and a client showing `0` for both is wrong.

`mechanics` describes what the **deck can do**, derived from its tags; `monarch` and `poison`
describe what **happened in this game**. A deck flagged `poison` whose seat dealt `0` did not use
it. Note that `mechanics.poison` keys off the proliferate tag as well, matching the web.

`player_url` and `deck_url` are web paths. A native client should navigate by `player_id` and
`deck_id` instead.



```json
{
  "id": 12, "date": "2026-08-30", "winner": {"id": 3, "name": "Alice"},
  "win_type": "combat", "ending_turn": 11, "note": null,
  "starting_player": {"id": 4, "name": "Bob"},
  "full_page_url": "/game/12",
  "participants": [{
    "player_id": 3, "player_name": "Alice",
    "deck_id": 7, "deck_name": "Atraxa", "commander": "Atraxa, Praetors' Voice",
    "art_url": "/art/atraxa.jpg", "won": true, "seat_position": 1,
    "salt_count": 0, "mana_fucked": false, "misplayed": false,
    "commander_damage": {"4": 21},
    "life_history": [[1756512000, 40], [1756512120, 33]],
    "player_accent": "var(--accent-3)", "player_url": "/player/3", "deck_url": "/deck/7"
  }]
}
```

#### `DELETE /api/games/{game_id}` · admin

`{"ok": true}`.

---

### 5.7 Legacy deck routes (non-API)

Two deck actions have no `/api` equivalent and are called on the web routes. Being non-API, they
**do** require a CSRF token.

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/deck/{deck_id}/retag` | Recompute deck tags |
| `POST` | `/deck/{deck_id}/remove-decklist` | Clear the decklist, keep the deck |

---

### 5.8 Pods

`_serialize_pod_summary`:

```json
{
  "id": 1, "name": "Kitchen Table", "slug": "kitchen-table",
  "is_active": true, "is_active_selection": true,
  "member_count": 5, "games_count": 42,
  "my_role": "podmaster",
  "can_manage": true, "can_switch": true,
  "can_retire": false, "can_restore": false, "can_delete": false
}
```

Detail adds `members` and `available_players`. `GET /api/pods` returns
`{"active_pod_id": 1, "can_create_pod": true, "pods": []}`.

| Route | Notes |
|---|---|
| `POST /api/pods` | Creator becomes podmaster |
| `PATCH /api/pods/{id}` | Rename; requires manage permission |
| `DELETE /api/pods/{id}` | Admin; blocked if the pod has games or is the default pod |
| `POST /api/pods/{id}/switch` | Sets the session's active pod |
| `POST /api/pods/{id}/retire` · `/restore` | Admin; the default pod cannot be retired |
| `POST /api/pods/{id}/members` | `{"player_id": 3, "role": "member"}` |
| `PATCH /api/pods/{id}/members/{player_id}` | `{"role": "podmaster"}` |
| `DELETE /api/pods/{id}/members/{player_id}` | Removing the last podmaster is refused |

---

### 5.8a Invites

All three management endpoints require `can_manage_pod` (podmaster or admin) and answer `403`
otherwise. Creating one additionally requires a verified email address — see
[Email verification as a gate](#email-verification-as-a-gate).

#### `POST /api/pods/{pod_id}/invites` · auth · 20/hour

```json
{ "expires_days": 7, "usage_limit": 1, "role": "member" }
```

Every field is optional. `expires_days` defaults to 7 and is clamped to 1–30; `usage_limit`
defaults to 1 and is clamped to 1–25; `role` is `member` or `podmaster`, and anything else
becomes `member`. Values are **clamped, not rejected** — a caller asking for 60 days gets 30.
A value that is not a number at all is a `400`.

`201`:

```json
{
  "id": 5, "pod_id": 4, "role": "member",
  "usage_limit": 1, "use_count": 0, "spent": false, "revoked": false,
  "expires_at": "2026-09-19T17:00:00", "created_at": "2026-09-12T17:00:00",
  "token": "<raw token>",
  "invite_url": "https://edh.figurensohn.de/invite/<raw token>"
}
```

**`token` and `invite_url` appear only here.** The server keeps a hash, so the link cannot be
shown again — if the user loses it, revoke the invite and create another. Hand it straight to a
share sheet.

#### `GET /api/pods/{pod_id}/invites` · auth

`{"invites": [...]}`, newest first, each entry as above **without** `token` or `invite_url`.
Use `revoked` and `spent` to decide what is still live.

#### `POST /api/pods/{pod_id}/invites/{invite_id}/revoke` · auth

`200` with the updated invite. Idempotent; an already-revoked invite is not an error.

#### `GET /api/invite/{token}` · public

What to show someone before they commit — deliberately reachable with no session, because that
is the whole point of an invite link.

```json
{
  "pod": { "id": 4, "name": "Friday Crew", "slug": "friday-crew" },
  "role": "member",
  "expires_at": "2026-09-19T17:00:00",
  "uses_remaining": 1
}
```

`410` when the invite cannot be used. Revoked, expired, fully spent, belonging to a retired pod,
and never having existed all return the **same** body, so this cannot be used to discover which
tokens are real.

#### `POST /api/invite/{token}` · auth · 10/hour

Joins the pod and makes it the session's active pod.

```json
{ "pod": {...}, "role": "member", "already_member": false }
```

Requires a session — unlike the web route, which can register someone inline. A client should
call `POST /api/register` first, then accept. `401` if there is no session, `410` if the invite
is unusable.

Accepting twice on the same account is **idempotent**: `already_member` comes back `true` and no
further use is spent, so re-opening the link does not burn the invite.

---

### 5.8b Guest players

#### `POST /api/pods/{pod_id}/guests` · auth

For someone who turned up to one game night and has no account.

```json
{ "name": "Visiting Dave" }
```

`201 {"player_id": 31, "name": "Visiting Dave", "pod_id": 4, "is_guest": true}`

The created `Player` has no `user_id`, no email, and membership in **this pod only**. It is a
record about a person who never agreed to anything, so keep that scope in mind before surfacing
guests anywhere outside the pod that created them.

**If a guest asks about their data.** They have no account, so there is no self-service path. The
answer is an administrator acting on their behalf:

- **See it** — `GET /api/players/{player_id}` and `GET /api/players/{player_id}/export`.
- **Correct it** — `PATCH /api/players/{player_id}` (admin) renames the player.
- **Erase it** — `DELETE /api/players/{player_id}` works only while they have no recorded games;
  once they have played it returns `409`, because deleting the row would rewrite other people's
  game history. The intended answer then is **rename to something non-identifying**, which
  removes the personal data while leaving the game records intact.

That is a deliberate position, not an oversight: the alternative is either losing other
participants' game history or keeping a name nobody can remove. Nothing records *who* added a
given guest, so there is no way to trace a request back to whoever entered the name.

`400` with `field: "name"` when the name is blank or over 100 characters · `403` unless you can
manage the pod.

---

### 5.8c Public game recaps

#### `POST /api/games/{game_id}/shares` · auth · 20/hour

Mints an **unauthenticated** URL for one game. Requires a verified email address, and that **you
actually played in this game** — pod membership is enough to *read* a game, but publishing it is a
decision about the participants' own data, and someone who was not at the table has no standing to
make it for the people who were. Administrators are exempt.

```json
{ "error": "Only someone who played in this game can publish it.", "reason": "not_a_participant" }
```

`403` with that `reason`. Any participant may publish — not only whoever recorded the game — since
any device at the table may have submitted the result.

```json
{ "show_player_names": true, "show_deck_names": true }
```

Both default to `false`. That is deliberate: a caller that sends nothing publishes the least it
can, not the most. With `show_player_names` off the seats read `Player 1`, `Player 2`, …; with
`show_deck_names` off the deck becomes `Private deck` and the commander is withheld.

`201`:

```json
{
  "id": 2, "game_id": 17,
  "show_player_names": true, "show_deck_names": true,
  "revoked": false, "created_at": "2026-09-12T17:00:00",
  "token": "<raw token>",
  "share_url": "https://edh.figurensohn.de/r/<raw token>"
}
```

As with invites, `token` and `share_url` are returned only on creation.

**This publishes other people's game data to the open web.** The switches are the only control
the people in that game have, so present them as a real choice before publishing, not as
defaults buried in a confirmation.

#### `GET /api/games/{game_id}/shares` · auth

`{"shares": [...]}` without tokens. Shows what is currently published so a client can offer to
revoke it.

#### `POST /api/games/{game_id}/shares/{share_id}/revoke` · auth

`200` with the updated share. The public URL starts returning `404` immediately.

Revoking needs only access to the game, not participation: taking a link down is never the
dangerous direction, so anyone in the pod who can see a published recap can also unpublish it.

#### `GET /api/recap/{token}` · public

The JSON twin of the `/r/{token}` web page, shaped by that share's own switches — never richer
than what the page would show.

```json
{
  "game": { "id": 17, "date": "2026-09-12T19:00:00", "win_type": "combat", "ending_turn": 11 },
  "pod": { "name": "Friday Crew" },
  "participants": [
    { "player": "Alice", "deck": "Atraxa Superfriends", "commander": "Atraxa, Praetors' Voice",
      "won": true, "mmr_delta": 12, "salt_count": 0, "life_delta": -8 }
  ],
  "show_player_names": true,
  "show_deck_names": true
}
```

`404` when the token is unknown or the share was revoked.

---

### 5.8d Email verification as a gate

Two actions are blocked until the account's address is confirmed, matching the web routes:
creating a pod invite, and publishing a recap. Both reach people outside the account.

Publishing carries a second, independent gate — see `not_a_participant` above. The two are checked
in that order, so a bystander who is also unverified is told they did not play.

```json
{ "error": "Verify your email address first.", "reason": "email_unverified" }
```

`403` with that `reason`. Branch on `reason` rather than the message: it means "show the
verification banner and offer Resend", not "this user lacks permission". Administrators are
exempt. Playing, recording games, and everything else stay available while unverified.

---

### 5.9 Administration

All under `/api/admin` require `is_admin`. Self-targeting destructive actions return `409`.

| Route | Response |
|---|---|
| `GET /api/admin/users` | `{selected_pod_id, pods, pending_users, active_users, inactive_users}`; query `pod_id` |
| `PATCH /api/admin/users/{id}` | `{"user": {…}}` — edits username/display name, bumps `session_version` |
| `DELETE /api/admin/users/{id}` | `{"ok": true}` |
| `POST /api/admin/users/{id}/approve` | `{"ok": true}` |
| `POST /api/admin/users/{id}/deny` | `{"ok": true, "username": "…"}` |
| `POST /api/admin/users/{id}/deactivate` | `{"ok": true}` |
| `POST /api/admin/users/{id}/toggle-admin` | `{"ok": true, "is_admin": false}` |

Registration requests are podmaster-or-admin, not admin-only:

| Route | Response |
|---|---|
| `GET /api/registration-requests` | `{"requests": []}` |
| `POST /api/registration-requests/{id}/approve` | `{"ok": true}` |
| `POST /api/registration-requests/{id}/deny` | `{"ok": true, "username": "…"}` |

---

### 5.10 Live multiplayer game

The lifecycle is: **`POST /api/start_game`** to get a token → other devices **join** with it →
everyone **polls and patches state** → the client submits the finished game with
`POST /api/games`.

#### `POST /api/start_game` · auth

```json
{
  "participants": [{"player_id": 3, "deck_id": 7, "borrow": false}],
  "starting_player": 3,
  "timer_mode": "off",
  "timer_minutes_per_player": null,
  "timer_increment_seconds": null,
  "timer_seconds_per_turn": null
}
```

At least two participants. `timer_mode` is `off`, `chess_clock`, or `turn_timer`. Creates the
`ActiveGame`, seeds every player at 40 life, and returns `{"token": "…"}`.

#### `GET /api/join/{token}` · token

```json
{
  "token": "…",
  "participants": [{
    "player_id": 3, "deck_id": 7, "seat_position": 1,
    "player_name": "Alice", "deck_name": "Atraxa",
    "commander_art": "/art/atraxa.jpg", "commander_art_scale": "cover",
    "mechanics": {"poison": true},
    "borrowed_from_player_id": null
  }]
}
```

#### `POST /api/join/{token}` · token

`{"player_id": 3}` → `{token, player_id, participants, state}`. Claims a seat.

#### `GET /api/game/{token}/state` · token

```json
{
  "version": 17,
  "life": {"3": 34, "4": 40},
  "flags": {"3": {"mana_fucked": false, "misplayed": false, "salt_count": 1}},
  "card_state": {"3": {"statuses": {"monarch": true}, "counters": {"poison": 2},
                       "commander_damage": {"4": 7}}},
  "turn": 5,
  "active_player_id": 4
}
```

`version` increases monotonically. Apply an incoming state only when its `version` exceeds the last
one applied.

#### `POST /api/game/{token}/state` · token

Partial update; every field optional.

```json
{ "player_id": 3, "life_delta": -3, "flags": {}, "card_state": {}, "turn": 6,
  "active_player_id": 4 }
```

Returns the full new state. `life_delta` must be within ±1000.

**Retry `503` with backoff** — see section 3. Because `life_delta` is relative, reconcile against
`version` rather than resending blindly.

The server appends a `[timestamp, life]` sample per actual life change, capped per player. To keep
them on the finished game, pass this token as `game_token` when you `POST /api/games`.

---

### 5.11 Cards, gallery, and import helpers

| Route | Auth | Notes |
|---|---|---|
| `GET /api/card-art?name=` | public | `{image, failure_reason, error_code}`; `404` when not found |
| `GET /api/gallery-image?path=` | public | Raw image bytes |
| `GET /api/cards/autocomplete?q=&source=` | public | `{"data": ["name", …]}`; `source` is `scryfall`, `custom`, or `all` |
| `GET /api/cards/named?exact=` | public | Scryfall or gallery card JSON; `400` without `exact` |
| `GET /api/cards/prints?name=` | **auth** | `[{id, set, set_name, collector_number, art_crop, normal}]` |
| `GET /api/ccauto/sets` | **auth** | Custom gallery sets |
| `GET /api/ccauto/sets/{set_name}` | **auth** | Cards in a set |
| `POST /api/commander-bracket` | public | `{cards: []}` or `{decklist_text}` → `{bracket, score}` |
| `POST /api/deck-import-parse` | auth | `{raw_import}` → commander(s), `sections`, `decklist_text` |
| `POST /api/deck-import-preview` | auth | `{raw_import}` → commander(s) only |

---

## 6) Recommended client startup sequence

1. `POST /api/login`, persist cookies.
2. `GET /api/me` to confirm the session (also on every cold start with a restored cookie).
3. `GET /api/pods` and `POST /api/pods/{id}/switch` if the user changes pod.
4. Load `/api/stats`, `/api/players`, `/api/decks`, `/api/games` as the UI needs them.
5. `GET /api/mobile/releases/android/latest` for an update check — **after** login, since it is
   authenticated.

On any `401`, discard the cookie and return to step 1.

---

## 7) Web-only surface with no JSON API

These product features exist only as HTML routes today. A standalone client cannot use them:

- Per-pod branding and scoring configuration
- Account data export (`/account/export`)

Adding JSON equivalents is phase 6 of `edh-son-android/INTEGRATION-PLAN.md`. Registration, email verification, and password reset were phase 3 and now have JSON
equivalents — see [5.1a Onboarding](#51a-onboarding). Invites, guest players, and recap
sharing were phase 4 — see [5.8a](#58a-invites), [5.8b](#58b-guest-players) and
[5.8c](#58c-public-game-recaps).
