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
