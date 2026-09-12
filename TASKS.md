# TASKS.md — Review Backlog (2026-07-01)

Findings from a three-part review (security, Python logic, frontend/templates). Every task
below was verified against the source. Each task is self-contained: defect, location, fix,
and verification. Pick up any task in isolation.

**Severity tiers:** P0 = fix before next deploy · P1 = fix soon · P2 = correctness/polish.

**Suggested model per task is noted** (`[sonnet]` for mechanical/localized fixes,
`[opus]` for design judgment or concurrency). All are small unless noted.

Status legend: `[ ]` todo · `[~]` in progress · `[x]` done

---

## P0 — Stored XSS (any pod member can run JS in another member's browser)

### [x] TASK-R01 — XSS via `json.dumps | safe` in play/manual game setup  `[sonnet]`
- **Where:** `templates/play_game.html:157`, `templates/manual_game.html:64`
  (source: `app.py` `play_game` ~6264 and `manual_game` ~7260 build `decks_json = json.dumps(decks_by_player)`).
- **Defect:** `{{ decks_json | safe }}` — `json.dumps` does NOT escape `</script>`. `decks_by_player`
  contains every player name and deck name (free-text user input). A name like
  `</script><script>…</script>` breaks out and runs arbitrary JS on page load, no interaction needed.
- **Fix:** In the route, pass the dict (not a json string) and render `{{ decks_by_player | tojson }}`
  in the template; drop the manual `json.dumps` and the `| safe`. Flask's `tojson` is script-safe.
  Mirror the existing safe pattern in `templates/player_panel.html:76-79`.
- **Verify:** Create a deck named `</script><script>alert(1)</script>`; load `/play_game` and
  `/manual_game`; confirm no alert and the JS `decksData`/`DECKS_BY_PLAYER` still parses.

### [x] TASK-R02 — XSS via player name re-injected through `innerHTML` in life counter  `[sonnet]`
- **Where:** `templates/life_counter.html:568-582` (`renderCommanderDamageSources`).
- **Defect:** `sourceName` is read via `.textContent` (which decodes Jinja's HTML-escaping back to
  the raw string) then interpolated into `row.innerHTML`. A malicious display name executes on the
  main game screen for every participant on load.
- **Fix:** HTML-escape `sourceName` before interpolation, or build the row with
  `document.createElement` + `textContent` instead of `innerHTML`. Reuse the `escapeHtml` helper
  pattern from `templates/player_panel.html:235-237` (port it into this template).
- **Verify:** Same payload name; open a live game with that player; confirm no alert and the
  commander-damage chips render the literal name text.

### [x] TASK-R03 — XSS via player name in compare bar  `[sonnet]`
- **Where:** `templates/players.html:195,200` (data attrs at `:57-58`, `:124-126`).
- **Defect:** `btn.dataset.playerName` returns the decoded raw name; interpolated into
  `namesEl.innerHTML` without re-escaping.
- **Fix:** Escape before interpolation, or set names via `textContent` on child spans.
- **Verify:** Select two players (one with an HTML-payload name) to compare; confirm no execution.

---

## P0 — Broken authorization (IDOR)

### [x] TASK-R04 — Deck mutation routes have no ownership check  `[sonnet]`
- **Where:** `app.py:4552-4624` — `/delete_deck/<id>`, `/deck/<id>/retire`, `/deck/<id>/unretire`,
  `/deck/<id>/plan`, `/deck/<id>/unplan`.
- **Defect:** All five load the deck by id and mutate/delete with NO ownership check. Any logged-in
  user can delete or retire anyone's deck by guessing/enumerating ids.
- **Fix:** Add the same guard already used in `update_deck` (`app.py:6100`) and
  `api_deck_detail` (`app.py:9189/9202`):
  `if not u.is_admin and (not u.player or deck.player_id != u.player.id): flash(...); return redirect(...)`.
  Apply to all five routes before mutating. (Get the current user the same way `update_deck` does.)
- **Verify:** As non-owner non-admin user B, POST `/delete_deck/<A's deck id>` → blocked with a
  permission flash; as the owner or admin → still works.

### [x] TASK-R05 — `/delete_player/<id>` is not scoped  `[sonnet]`
- **Where:** `app.py:4627-4663`.
- **Defect:** Any logged-in user can delete any non-user-linked Player (cascades to their decks).
  The API equivalent (`app.py:8764`) at least requires admin; the web route is weaker.
- **Fix:** Require admin (or podmaster of the player's pod, matching `remove_pod_member` at
  `app.py:4523`) before deletion.
- **Verify:** Non-admin POST `/delete_player/<id>` → forbidden/flash; admin → works.

### [x] TASK-R06 — `/api/login` has no rate limit  `[sonnet]`
- **Where:** `app.py:7770` (`api_login`). `/login` has `@limiter.limit("10 per minute")` at `app.py:3329`.
- **Defect:** Unlimited password brute-force via the JSON API, bypassing the web login limit
  (same session cookie, same `check_password_hash`).
- **Fix:** Add `@limiter.limit("10 per minute", methods=["POST"])` (or matching policy) to `api_login`.
- **Verify:** 11 rapid POSTs to `/api/login` → 429 on the 11th.

---

## P1 — Crashes / broken endpoints (currently 500)

### [x] TASK-R07 — `api_player_detail` references undefined `current_user`  `[sonnet]`
- **Where:** `app.py:8750, 8765` (`api_player_detail`).
- **Defect:** `current_user` is never assigned in this function (no global `current_user` exists).
  Every `PATCH`/`DELETE /api/players/<id>` raises `NameError` → 500. Endpoint fully broken for
  its two mutating methods (fails closed, so not an auth bypass — just non-functional).
- **Fix:** Add `current_user = get_current_user()` at the top of the function (as every sibling
  handler does, e.g. `api_deck_detail` at `app.py:9183`).
- **Verify:** Admin PATCH `/api/players/<id>` renaming a player → 200; DELETE an unused
  non-user-linked player → success.

### [x] TASK-R08 — Deck import crashes on `"oracleCard": null`  `[sonnet]`
- **Where:** `deck_import.py:225` (Archidekt), `deck_import.py:449-453` (Moxfield merge).
- **Defect:** `.get("oracleCard", {}).get("name")` — if the API returns `"oracleCard": null`
  (proxy/custom/un-mapped cards), the `{}` default is NOT used, so it's `None.get(...)` →
  `AttributeError`. Callers only catch `DeckParserError`, so it becomes an unhandled 500 on
  `/add_deck`, `/deck/<id>/update`, `/api/deck-import-preview`, `/api/decks`, etc.
- **Fix:** Use `(card_obj.get("oracleCard") or {}).get("name")` in both spots.
- **Verify:** Add a unit test feeding a card dict with `"oracleCard": None`; parser returns the
  fallback `name` instead of raising.

### [x] TASK-R09 — `/add_game` renders a missing template  `[sonnet]`
- **Where:** `app.py` route rendering `add_game.html` (no such template exists).
- **Defect:** Hitting `/add_game` → `TemplateNotFound` → 500. No UI links to it (dead route).
- **Fix:** Remove the dead route (preferred — nothing references it), or add the template if it
  was meant to exist. Confirm via grep that nothing calls `url_for` to it before removing.
- **Verify:** `grep -rn add_game templates/ app.py` shows no live reference; route gone.

### [x] TASK-R10 — Unguarded `int()` on form fields in manual/record game  `[sonnet]`
- **Where:** `app.py:7276-7277, 7291` (`manual_record_game`) and `7362-7363, 7377` (`record_game`).
- **Defect:** `int(p_id)` / `int(d_id)` with no try/except; a non-numeric form value → `ValueError`
  → 500 instead of a clean 400. Contrast `/api/start_game` (`app.py:6480-6485`) which wraps these.
- **Fix:** Wrap the casts in try/except `(KeyError, TypeError, ValueError)` and flash/return 400.
- **Verify:** POST the form with `player0=abc` → clean validation error, not a 500.

---

## P1 — Live-game data loss

### [x] TASK-R11 — Lost-update race in game-state polling API  `[opus]`
- **Where:** `app.py:7008-7167` (`api_game_state` POST).
- **Defect:** Read-modify-write of the whole `state_json` blob with no row lock and no
  optimistic-concurrency check against the client's version. Two concurrent POSTs (4 players
  polling/POSTing every few seconds) both read the same base state; the second commit silently
  discards the first player's change (dropped life/flag/counter updates). `state["version"]` is
  incremented but never validated against an incoming expected version.
- **Fix (design):** Add optimistic concurrency — reject/merge when the client's base `version`
  doesn't match the stored one (return the fresh state so the client re-applies), and/or take a
  row lock for the read-modify-write. Prefer per-key merge over whole-blob overwrite. This one
  needs judgment about the client contract — coordinate the fix with `life_counter.html` /
  `player_panel.html` push logic.
- **Verify:** Simulate two overlapping POSTs (player A life delta, player B flag toggle) against
  the same base version; both changes survive.

---

## P2 — Correctness (silent wrong results)

### [x] TASK-R12 — Stale APK served over a newer one  `[sonnet]`  *(has a failing test already)*
- **Where:** `app.py:3508-3549` (`_find_manifest_android_release_artifact` /
  `_find_latest_android_release_artifact`).
- **Defect:** The manifest's referenced APK is returned as long as it still exists on disk; a newer
  APK dropped in the directory is never served until the manifest is regenerated. Proven by the
  failing test `tests/test_apk_release.py::test_prefers_newest_apk_in_directory_when_manifest_is_stale`.
- **Fix:** Compare the manifest artifact's version against the newest on-disk APK and prefer the
  newer; fall back to manifest only when it is newest/only.
- **Verify:** The named test passes (expects `0.6.3+9`, currently returns `0.6.2+8`).

### [x] TASK-R13 — `POST /api/games` never updates MMR  `[opus]`
- **Where:** `app.py:8924-8971`. Contrast `/end_game` (`6936-6981`) and `/manual_record_game`
  (`7306-7343`) which compute deltas.
- **Defect:** Games recorded via the REST API create Game/GameParticipant rows with no MMR
  computation — leaderboards silently diverge by entry point.
- **Fix:** Factor the MMR update from `/end_game` into a shared helper and call it here too.
  (Needs care to match the existing delta/history write shape.)
- **Verify:** Record a game via `POST /api/games`; deck `mmr`/`mmr_history_json` and
  `Game.mmr_deltas_json` update just as they do via `/end_game`.

### [x] TASK-R14 — `/saltmine` MMR leaderboard ignores pod scoping  `[sonnet]`
- **Where:** `app.py:3874-3891`.
- **Defect:** The MMR-leaderboard query joins Game with no `Game.id.in_(scoped_game_ids)` filter,
  unlike every other block on the page (e.g. `3716-3720`). On multi-pod installs it counts games
  from every pod even when scoped to one.
- **Fix:** Apply the same `scoped_game_ids`/`game_q` filter used by the sibling queries.
- **Verify:** Two pods with disjoint games; scope to pod A; leaderboard counts only pod A's games.

### [x] TASK-R15 — Wrong "won" attribution for borrowed decks  `[sonnet]`
- **Where:** `app.py:7454` (`_serialize_deck_detail`), `"won": game.winner_id == deck.player_id`.
- **Defect:** Uses the deck's current owner instead of the participant who played it (`gp.player_id`).
  For borrowed decks the per-game "won" flag in `/api/decks/<id>` is wrong. `_serialize_deck_summary`
  (`7412-7416`) does it correctly.
- **Fix:** Compare `game.winner_id == gp.player_id` for the participant row in the loop.
- **Verify:** A game where deck was borrowed by a different player who won; `/api/decks/<id>` shows
  `won: true` for that game.

### [x] TASK-R16 — Commander-bracket thresholds disagree with CLAUDE.md  `[opus]`
- **Where:** `app.py:1906-1943` vs CLAUDE.md ("Commander Bracket System").
- **Defect:** Code maps `score>=7→5, >=4→4, >=2→3, else 2/1`; doc says `0=1, 1–2=2, 3–4=3, 5–7=4, 8+=5`.
  They disagree (score 2 → code says 3, doc says 2). One is wrong.
- **Fix:** Decide the intended mapping (design call), then align code and doc. If code changes tag
  logic, bump `DECK_TAGS_VERSION` per CLAUDE.md conventions.
- **Verify:** Add a unit test pinning a few `score → bracket` pairs to the agreed mapping.

---

## P2 — Polish / robustness

### [x] TASK-R17 — Mojibake in source constants  `[sonnet]`
- **Where:** `app.py:84` (`DEFAULT_POD_NAME`), `app.py:6680-6687` (game status icons),
  `app.py:1894` (dead fallback string).
- **Defect:** Double-encoded UTF-8 baked into the source. Fresh installs get a garbled default pod
  name; the live-game screen shows garbled text instead of emoji (👑 ⚔️ 🏙️ ⚡ ✨ ☠️).
- **Fix:** Replace the corrupted literals with the correct UTF-8 characters. Save as UTF-8.
- **Verify:** `DEFAULT_POD_NAME == "Der Keller – Die Salzmine"`; icons render as emoji in a game.

### [x] TASK-R18 — Silent sync failures with no retry  `[opus]`
- **Where:** `life_counter.html:2042-2059, 2150-2167` (`syncPushLifeForPlayer`,
  `syncFlushLifeChanges`) and `player_panel.html:291-304` (`pushUpdate`).
- **Defect:** Local state is marked "sent" before the fetch resolves; errors are swallowed with an
  empty catch and never retried. A dropped POST (common on mobile) silently desyncs that device.
- **Fix:** Only mark as sent after a confirmed response; retry with backoff (reuse the existing
  `_syncFailCount`/backoff pattern for polling), or surface a "sync failed" indicator.
- **Verify:** Simulate a failed POST; confirm the change is retried and eventually propagates, or a
  visible warning appears.

### [x] TASK-R19 — Commit-without-rollback in several deck routes  `[sonnet]`
- **Where:** `app.py:5747-5748, 5970, 9154-9155, 9195-9196, 9312` (`add_deck`, `retag_deck`,
  `api_decks` POST, `api_deck_detail` DELETE/PATCH).
- **Defect:** `db.session.commit()` with no try/except/rollback (inconsistent with `update_deck`'s
  rollback-and-restore at `6160-6185`). A DB failure → unhandled 500, no rollback, skips art cleanup.
- **Fix:** Wrap commits in try/except with `db.session.rollback()` and a logged error; mirror
  `update_deck`'s pattern. Also fix the orphaned-art-on-failure leak noted at `app.py:6132-6191`.
- **Verify:** Force a commit failure (e.g. locked DB in a test) → clean error + rollback, no leak.

### [x] TASK-R20 — `upload-art` returns 200 with `{"url": null}` on empty upload  `[sonnet]`
- **Where:** `app.py:5663-5698` (`/api/decks/<id>/upload-art`); `_store_custom_art_upload`
  (`app.py:1719`) returns `None` on empty filename.
- **Defect:** Empty-filename upload falls through to `commit()` and returns 200 with a null url —
  a silent "success" that stored nothing (violates the project's "never silent" rule).
- **Fix:** Return 400 when `local_path` is None / filename is empty.
- **Verify:** POST with no file → 400 with a clear message.

### [x] TASK-R21 — Falsy-zero filter bugs on `/games`  `[sonnet]`
- **Where:** `app.py:4728-4735` (`min_players`/`max_players`), `4836-4837`
  (`avg_turns`/`avg_duration`).
- **Defect:** `if min_players or max_players:` treats `0` as "unset"; `?max_players=0` returns
  unfiltered instead of zero rows. `round(avg, 1) if avg else None` reports a true average of `0`
  as "no data".
- **Fix:** Use `is not None` checks for the filter params; distinguish `0` from "no matching games"
  in the averages (guard on the count, not the value).
- **Verify:** `/games?max_players=0` returns no rows; a set of games all with `duration_seconds=0`
  reports `0.0`, not `None`.

### [x] TASK-R22 — Remove stray non-template files from `templates/`  `[sonnet]`
- **Where:** `templates/watcher.py`, `templates/SOUL.md`, `templates/relationships.md`,
  `templates/life_counter.html.bck`.
- **Defect:** Not Flask templates and not referenced anywhere (`watcher.py` is byte-identical to
  `.agent-sync/watcher.py`). Dead weight in Jinja's search path; `.bck` risks editing the wrong file.
- **Fix:** Delete them (confirm no `render_template`/import references first).
- **Verify:** App still boots and all pages render; `git grep` finds no references.

---

## P2 — From resolved Veto Buffer (2026-07-01, best-judgment defaults pending user confirm)

### [x] TASK-R23 — Set explicit session-cookie security flags  `[sonnet]`  *(resolves VETO-R01 = A)*
- **Where:** app config near the Flask app/secret-key setup (`app.py` ~49-58 area); CSRF exemption
  at `app.py:3192-3208`.
- **Decision:** Keep `/api/*` CSRF-exempt but make the protection explicit instead of relying on
  browser SameSite defaults.
- **Fix:** `app.config.update(SESSION_COOKIE_SAMESITE="Lax", SESSION_COOKIE_SECURE=True)`.
  Gate `SECURE` on HTTPS/production (e.g. an env flag) so local HTTP dev on `:5001` still works.
  Do NOT add a required CSRF header on `/api/*` — session-cookie mobile/native clients (docs/API.md)
  would break.
- **Verify:** Response `Set-Cookie` includes `SameSite=Lax` (and `Secure` when the HTTPS flag is on);
  web login and `/api/login` still work; existing tests still pass.

### [x] TASK-R24 — Document intentional cross-pod ("career") stats  `[sonnet]`  *(resolves VETO-R02 = A)*
- **Where:** `app.py:4976-5215` (`player_detail`, `compare_players`, `api_compare`).
- **Decision:** Cross-pod stats on these pages are intentional (lifetime career view), unlike the
  pod-scoped `/games`, `/saltmine`, `/api/stats`.
- **Fix:** Add a short comment at each of the three functions stating the non-scoping is deliberate
  (career/lifetime view) so a future review doesn't re-flag it as the R14-style bug. No behavior change.
- **Verify:** Comments present; no functional diff. (If you later prefer pod-scoping, that's the
  alternate VETO-R02 = B — filter through `game_query_for_scope()`.)

### [x] TASK-R25 — Run under gunicorn as a non-root user in Docker  `[sonnet]`  *(resolves VETO-R03 = A)*
- **Where:** `Dockerfile` (CMD `python app.py`, runs as root), `requirements.txt`, `app.py:9372`.
- **Decision:** Serve via a production WSGI server and drop root.
- **Fix:** Add `gunicorn` to `requirements.txt`; in the `Dockerfile` create a non-root user, add a
  `USER` directive, and change CMD to
  `["gunicorn","-b","0.0.0.0:5000","-w","4","app:app"]`. Keep `app.run(...)` guarded under
  `if __name__ == "__main__":` for local dev. Ensure the app object is importable as `app:app`
  (module `app`, Flask instance `app`) — confirm the instance name.
- **Verify:** `docker compose up --build -d commander-tracker-test` (port 5001) boots under gunicorn;
  container process is non-root (`docker exec … whoami` ≠ root); pages return 200.

---

## P1 — Rate limits (added 2026-09-12, from the Phase 3 onboarding security review)

### [x] TASK-R26 — Rate limits are per-worker, so every advertised limit is ~4x too high  `[opus]`
- **Where:** `app.py:239` (`limiter = Limiter(..., storage_uri="memory://")`), `Dockerfile` (`gunicorn -w 4`).
- **Defect:** The limiter stores counters in process memory, and the container runs four gunicorn
  workers, each with its own independent count. Flask-Limiter's `memory://` backend shares nothing
  across processes, so a caller whose requests land on different workers — which happens under any
  concurrency, with no special tooling — gets roughly four times the stated ceiling. The documented
  `5 / hour` on `POST /api/register`, `/api/password/forgot` and `/api/email/verify/resend` is really
  about 20/hour; the `10 / hour` on `/api/password/reset` and `/api/email/verify` is about 40/hour.
  `POST /api/login` at `10 / minute` is likewise about 40/minute.
- **Why it matters now:** this line predates the JSON API, but Phase 3 made it load-bearing. Three
  deliberate design decisions lean on these limits being real: `/api/password/forgot` answering
  identically for known and unknown addresses, unknown and expired tokens being indistinguishable,
  and `POST /api/register` disclosing username/email collisions (409 with a `field`). The last one
  was accepted on the grounds that it matches the long-standing web form and is throttled to
  5/hour — at ~20/hour, collision disclosure becomes a meaningfully cheaper enumeration oracle.
  Reset-token guessing is not a practical threat either way (256-bit `secrets.token_urlsafe(32)`),
  so the exposure is enumeration and account-creation spam, not token brute force.
- **Fix:** Point `storage_uri` at a store shared across workers. Options, in order of preference:
  1. Redis (`redis://`) — the backend Flask-Limiter is designed around, but adds a service to the
     compose stack for one feature.
  2. The existing SQLite file via a limiter table — no new service, but write contention interacts
     with the `BEGIN IMMEDIATE` serialization from ADR-014; measure before choosing this.
  3. `gunicorn -w 1 --threads N` — makes the current limits honest with no new dependency, at the
     cost of losing process-level parallelism. Cheapest correct option if the traffic allows it.
  Whichever is chosen, re-read the field-disclosure decision on `/api/register` afterwards.
- **Resolved with option 3** (2026-09-12): `-w 1 --threads 8`. Measured before and after with
  12 concurrent requests against the 5/hour `/api/password/forgot` limit — 12/12 accepted on
  four workers, 5 accepted and 7 rejected on one. The `/api/register` collision disclosure is
  therefore genuinely throttled as originally assumed and needs no revisiting.
- **Verify:** With four workers running, hammer `POST /api/password/forgot` from one IP and confirm
  the 6th request in an hour returns `429`, not the ~21st. A test that asserts this needs to go
  through the real gunicorn container, not the single-process test client — the in-process suite
  cannot observe the bug. `tests/test_api_onboarding.py::test_registration_is_rate_limited` proves
  the decorator is wired up, not that the ceiling holds across workers.
- **Do not** relax the documented limits in `docs/API.md` to match the broken behaviour; the
  documented numbers are the intended contract.

### [x] TASK-R27 — A mail-send failure 500s over an account that was already created  `[sonnet]`
- **Where:** `app.py` `api_register` (commit, then `send_email_verification`), `send_transactional_email`.
- **Defect:** `send_transactional_email` has no `try/except` around `smtplib.SMTP(...)`. In
  `api_register` the order is `db.session.commit()` -> send mail -> `_establish_session(user)`. If the
  relay is down, times out, or rejects auth, the account, player and pod are already committed, but
  the handler raises before the session is established. The client gets a 500, is not signed in, and
  has no way to know an account now exists under the username and email it submitted. An automatic
  retry then hits the 409 "already taken" path and looks like someone else took the name.
- **Note:** the ordering is pre-existing — the web `register()` route has always worked this way —
  but a mobile client retries where a human re-reading a flashed message does not, and "created,
  but reported as failed" is worse in an API contract than in a form post.
- **Fix:** Wrap the mail step so a mail outage degrades instead of failing the request: log the
  exception (never the token), still call `_establish_session`, and return `201` with something the
  client can act on (e.g. `"verification_email_sent": false`) so the UI can offer Resend rather than
  claim an email is on its way. Apply the same treatment to the web route and to
  `accept_pod_invite`, which has the same shape. Document the new field in `docs/API.md`.
- **Verify:** Point `SMTP_HOST` at a closed port, `POST /api/register`, and confirm `201`, a working
  session, `verification_email_sent: false`, and an error in the log with no token in it.

### [x] TASK-R28 — No length bounds on `username`, `display_name`, `pod_name`  `[sonnet]`
- **Where:** `app.py` `validate_registration`.
- **Defect:** Presence, email shape, password rules and uniqueness are all checked, but length never
  is. SQLite treats `db.String(100)` as type affinity, not a constraint, so a client can persist
  strings up to the global 15 MB `MAX_CONTENT_LENGTH` into these columns. No injection risk (the ORM
  parameterizes everywhere) — this is storage bloat and rendering cost wherever the values are later
  displayed. Pre-existing and shared with the web form; the JSON endpoint makes it scriptable.
- **Fix:** Cap each field in `validate_registration` at the column width, returning the existing
  per-field `RegistrationProblem` so both the form and the API report it the same way.
- **Verify:** A 10,000-character `pod_name` returns `400` with `field: "pod_name"` and writes nothing.

### [x] TASK-R29 — `_normalize_life_history` validates every sample before truncating  `[sonnet]`
- **Where:** `app.py` `_normalize_life_history` (the per-participant loop, then the
  `[-MAX_LIFE_HISTORY_SAMPLES:]` slice).
- **Defect:** Each `[timestamp, life]` pair is type- and bounds-checked before the list is trimmed to
  120. A payload bounded only by the 15 MB body cap can pack on the order of a million trivial pairs
  per participant, across six seats, and force the whole validation pass for data that is then thrown
  away. Bounded work, so denial of service is not really on the table, but it is wasted CPU on a
  path a client controls.
- **Fix:** Reject outright when the incoming list is longer than some sane multiple of
  `MAX_LIFE_HISTORY_SAMPLES` before iterating it.
- **Verify:** A 100,000-sample submission is rejected without the per-element loop running; a normal
  submission of a few hundred samples still truncates to the newest 120 as today.

---

## P2 — Privacy follow-ups (added 2026-09-12, from the Phase 4 compliance review)

The review returned WARN on six items and blocked on none. The one it called worth fixing
pre-merge — recap publishing being open to any pod member rather than the game's own
participants — was fixed in the Phase 4 commit. These are the rest. GDPR is the only declared
scope in `INIT.md`; all of these are judgment calls at this scale, not violations.

### [ ] TASK-R30 — Co-participants are not told when a game they played is published  `[opus]`
- **Where:** `app.py` `api_publish_game_recap`, `publish_game_recap`.
- **Issue:** Publishing is now restricted to participants, but it is still *one* participant
  deciding for everyone at the table. The others are not notified and cannot revoke a share of a
  game they were in unless they happen to look at that game's detail screen. Legal basis for
  publishing their data rests on legitimate interest, which is arguable for a friend group but
  not settled.
- **Fix:** Notify the other participants in-app when a game they played in gets a public link,
  and let any participant revoke a share on a game they were in. Revoking is already open to
  anyone with pod access, so the gap is really the notification.
- **Why P2:** among friends who all consented to being in a shared pod, this is a trust and
  courtesy matter more than a compliance one. It becomes important the moment a pod contains
  someone who is not a close friend.

### [ ] TASK-R31 — No record of who added a guest player  `[sonnet]`
- **Where:** `app.py` `Player` model, `api_create_guest_player`, `create_guest_player`.
- **Issue:** A guest row holds a third party's name with no `added_by_user_id`, so if that person
  ever asks about the record there is no way to tell who entered it or which pod member to ask.
- **Fix:** Add a nullable `added_by_user_id` FK to `Player`, set it on both guest-creation paths,
  and add it to the `schema_migrations` bootstrap. Cheap if this model is being touched anyway.
- **Note:** the handling answer itself is now written down in `docs/API.md` §5.8b — administrator
  renames the player, since `DELETE` is refused once the guest has recorded games.

### [ ] TASK-R32 — Nothing expires; write down that this is deliberate  `[haiku]`
- **Where:** `docs/` (no code change).
- **Issue:** Revoking a share or invite sets `revoked_at` but the row, and the underlying game and
  guest data, are kept forever. The review flagged this under storage limitation, while noting
  that permanent game history is arguably the product's whole purpose.
- **Fix:** One short retention note in the docs stating that game history is retained
  indefinitely by design, that revoked shares and invites are kept as an audit record rather than
  deleted, and that account deletion anonymizes rather than removes. Documenting the intent is
  the whole task; no behaviour change.

---

## P2 — Deferred product decisions (added 2026-09-12)

### [ ] TASK-R33 — `Pod.scoring_json` has no schema and no consumer  `[opus]`
- **Where:** `app.py` `Pod.scoring_json`, `pod_scoring()`, `_apply_pod_config()`.
- **Situation:** the column has existed since the SaaS foundation work but nothing anywhere
  reads it, there is no web UI for it, and no schema is written down. Phase 6 exposed it
  read-only on `GET /api/pods/{id}` (it round-trips whatever is stored) and made `PATCH`
  refuse it with `400 field: "scoring"`.
- **Why not just accept writes:** storing arbitrary JSON that nothing interprets is a sink,
  not a feature. Shipping a settings screen for it would have meant inventing a scoring
  system and presenting it as parity with the web, which has none.
- **What is needed first:** decide what per-pod scoring actually means for this product —
  points per win, per elimination, tie-breaks, whether it feeds MMR or sits beside it — then
  the schema, validation, and UI follow from that. It is a product question, not a coding one.
- **If the answer is "nothing":** drop the column in a migration rather than leaving a dormant
  field that reads like an unfinished feature.

Note: `enabled_mechanics_json` was in the same dormant state and *was* implemented in Phase 6,
because its meaning is unambiguous — the six mechanics already exist as `POD_MECHANIC_KEYS`
and `derive_deck_mechanics()`. Scoring is the one that needed a decision.

---

## Notes for whoever picks these up
- Test suite baseline: `40 passed, 8 failed`. Of the 8 failures, only
  `test_apk_release.py::…stale` is a real app bug (TASK-R12). The other 7 are test-harness issues
  (tests set `session["user_id"]` without `session["_csrf_token"]`, so the global CSRF/login guard
  redirects them) — worth fixing the tests separately, but they are NOT app defects.
- No SSRF, SQL injection, path traversal, or hardcoded-secret issues were found — those areas are
  in good shape (domain-allowlisted importer, sanitized art filenames, hashed passwords,
  random-fallback `FLASK_SECRET_KEY`, correctly-scoped game-state writes).
- Lower-confidence design questions (NOT tasked — confirm intent first): API CSRF exemption for
  `/api/*` relies on SameSite=Lax defaults (consider setting `SESSION_COOKIE_SAMESITE`/`SECURE`
  explicitly); cross-pod visibility on player/compare/detail pages may be intentional; Flask dev
  server used in Docker (`Dockerfile` CMD) — consider gunicorn/waitress for production.
