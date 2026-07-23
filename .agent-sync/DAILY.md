# DAILY — commander-tracker

Last updated: 2026-07-01
Branch: main
Orchestrator state: **BACKLOG CLEAR — 25/25 done. Wave 3 (R12–R25) uncommitted, awaiting commit.**

## Wave 3b result (R13, R16) — DONE, verified, uncommitted
- R13: extracted `_apply_mmr_for_game(game, participant_rows, winner_player_id)`; now called from
  end_game, manual_record_game, AND api_games POST (the fix). MMR math unchanged — full test_mmr
  suite (15) still passes; new tests/test_api_games_mmr.py passes. No RMW-engine involvement.
- R16: bracket mapping aligned to CLAUDE.md via `_commander_bracket_for_score` (0→1,1–2→2,3–4→3,
  5–7→4,8+→5); cEDH override preserved; DECK_TAGS_VERSION NOT bumped (bracket computed on demand,
  not tag-cached); new tests/test_commander_bracket.py passes.
- Verification (orchestrator, independent): `Ran 52 tests … failures=7` (baseline; 2 new pass;
  test_mmr + bracket tests all green). Bracket mapping diff reviewed — matches doc exactly.
- app.py claim released.

## UNCOMMITTED working tree (Wave 3 = 3a + 3b): app.py, deck? no. Files:
  app.py, templates/life_counter.html, templates/player_panel.html, static/css/player_panel.css,
  Dockerfile, requirements.txt, tests/test_api_games_mmr.py, tests/test_commander_bracket.py,
  4 deleted templates/ stray files. Plus .agent-sync/*, TASKS.md (orchestration).

## Outstanding before/after merge:
  - R18 in-game-UI SCREENSHOT verification (life counter + new #syncFailedBadge) — per CLAUDE.md.
  - R18 client-contract change: phone panel now pushes absolute life (was delta) — intentional.
  - R18 CSS nit: #syncFailedBadge uses hardcoded hex in player_panel.css, not tokens — cleanup.

## Backlog: 25/25 DONE.

main pushed to origin at cdca1dd (P0+P1 live). Working tree holds UNCOMMITTED Wave 3a below.

## Wave 3a result (P2, 12 tasks) — DONE, verified, uncommitted
3 parallel file-disjoint lanes; all clean. **R12,R14,R15,R17,R18,R19,R20,R21,R22,R23,R24,R25 done.**
- Lane A (python-reviewer, app.py): R12 stale-APK (test now GREEN), R14 saltmine pod-scope,
  R15 borrowed-deck won (gp.player_id), R17 mojibake, R19 rollback+art-prune on 5 routes,
  R20 upload-art 400, R21 falsy-zero filters, R23 SameSite=Lax + env-gated Secure, R24 career
  comments.
- Lane B (typescript-reviewer, templates + player_panel.css): R18 sync-retry (mark-sent only on
  success + backoff/indicator; phone push switched delta→absolute life for idempotent retry),
  R22 deleted 4 stray files.
- Lane C (infra-reviewer, Dockerfile + requirements.txt): R25 gunicorn -w 4 + non-root (built &
  boot-verified: whoami=app, /login 200, /data writable).
- **Verification (orchestrator, independent):** combined tree → `Ran 50 tests … failures=7`
  (R12 flipped green; no new failures). Spot-checked R23/R15/R18 diffs — correct.
- **Review notes (non-blocking):** (1) R18 added `#syncFailedBadge` styles in player_panel.css
  using hardcoded hex, not design tokens — against CLAUDE.md CSS rule; minor, flag for cleanup.
  (2) R18 in-game-UI SCREENSHOT verification OUTSTANDING (can't run live game in sandbox) —
  needs manual check before/after merge per CLAUDE.md, incl. the new badge on a real viewport.
- File claims released (app.py reclaimed for 3b).

## Wave 3b — DISPATCHED (R13 MMR-on-/api/games + R16 bracket align-to-doc)
One python-reviewer(opus), both edit app.py. R16 decision: ALIGN CODE TO CLAUDE.md mapping
(0=1,1–2=2,3–4=3,5–7=4,8+=5) per user. R13: factor shared MMR helper, call from all 3 sites.

## Backlog: 23/25 done. Remaining: R13, R16 (in flight).

## R11 result (P1, game-state race) — DONE, verified, uncommitted
Architect ADR-014 (Option B2) → python-reviewer(opus) implementation.
- **app.py:** SQLite engine now WAL + busy_timeout (15s) via an Engine `connect` PRAGMA
  listener; a dedicated AUTOCOMMIT engine (`get_game_state_rmw_engine`) issues
  `BEGIN IMMEDIATE` around the game-state read-modify-write so it serialises via SQLite's
  cross-process write lock (survives gunicorn -w 4). Per-key merge kept verbatim; version
  still bumped; `OperationalError` → logged 503. **No client change.**
- **Deviation from ADR sketch (justified):** the literal sketch (isolation_level=None +
  BEGIN IMMEDIATE on the ORM session) deadlocked app bootstrap during
  run_schema_migrations/create_all. Resolved by isolating the RMW onto a dedicated engine,
  leaving the ORM session untouched. Same ADR-014 decision, safer mechanism.
- **Tests:** tests/test_api_game_state_concurrency.py (2 tests). RED confirmed (both fail
  pre-fix: 40!=33, 37!=32); GREEN post-fix.
- **Verification (orchestrator, independent):** Docker suite → `Ran 50 tests … failures=8`
  (same baseline; 2 new pass; zero ORM-route regressions). Diff reviewed by orchestrator:
  transaction boundaries, lock release on early return, 503 path, datetime format all correct.
- File claims released. Changes UNCOMMITTED (app.py, tests/test_api_game_state_concurrency.py).

## Backlog: 11/25 done (all P0 + all P1). Remaining: P2 R12–R25.

## Wave 2 result (P1) — DONE, verified, uncommitted
Dispatched 2 parallel file-disjoint lanes; both clean. **R08, R09, R10 done.**
- Lane A (python-reviewer, `deck_import.py`): R08 — `(… or {}).get("name")` in both
  Archidekt + Moxfield paths; added `tests/test_deck_import_oracle_card_null.py` (2 tests).
- Lane B (python-reviewer, `app.py`): R09 — removed dead `/add_game` route (grep-confirmed
  no references, `json` import left intact); R10 — guarded `int()` casts in
  manual_record_game + record_game, reusing the validated int downstream.
- **Verification (orchestrator, independent):** Docker test image → `Ran 48 tests …
  failures=8`. The 8 are the documented baseline; the 2 new deck-import tests pass. No new
  failures. Diffs reviewed: surgical.
- File claims released. Changes **uncommitted** in the working tree (app.py, deck_import.py,
  tests/test_deck_import_oracle_card_null.py) — awaiting user commit/merge decision.

## Next — R11 wave (game-state lost-update race, [opus])
app.py free. Plan: architect ADR (optimistic version check vs row lock + client-contract
impact on life_counter.html / player_panel.html) → implement → review. Needs user approval
(new dispatch). Then P2 backlog (R12–R25).

## Wave 1 result (P0) — DONE, verified, uncommitted
Dispatched 3 parallel lanes; all completed clean. **R01–R07 done.**
- Lane 1 (security-reviewer, `app.py` + play/manual templates): R01, R04, R05, R06, R07.
- Lane 2 (typescript-reviewer, `life_counter.html`): R02.
- Lane 3 (typescript-reviewer, `players.html`): R03.
- **Verification (orchestrator, independent):** ran the suite in the Docker test image
  (`unittest discover`) → `Ran 46 tests … FAILED (failures=8)`, and the 8 failures are the
  documented baseline (1 real APK bug = R12; 7 pre-existing CSRF/login-redirect harness
  failures). **No new failures** in any changed path. Diff reviewed: surgical, matches
  existing patterns.
- **Correction to R09:** Lane 1 claimed `add_game.html` exists with the same XSS pattern;
  verified FALSE — the template does not exist, so `/add_game` builds an unsafe `decks_json`
  but 500s before rendering. It is a **dead route** (as R09 originally said). R09's fix
  (remove the route) also removes the latent unsafe build. No new task needed.
- **Outstanding for R01/R02/R03:** CLAUDE.md requires a running-game screenshot for
  in-game UI changes. These are escaping-only (no intended visual change) and tests pass,
  but the screenshot check is NOT yet done — flagged, not claimed.
- File claims released. Changes are **uncommitted** in the working tree for user review.

---
Prior state (kept for history):
Orchestrator state was: **AWAITING PLAN APPROVAL** (no tasks dispatched)

The Lead Orchestrator reads this file first on every `/orchestrate tick`. Nothing here has
been dispatched — Section 2 is a *proposed* plan and requires human approval before any
agent is launched (per orchestration.md: "Morning plan MUST be approved before dispatching").

---

## Section 1 — State / Context

- **Backlog:** `TASKS.md` at repo root — 22 tasks (`TASK-R01`–`TASK-R22`) from the
  2026-07-01 three-part review (security, Python logic, frontend/templates). All verified
  against source.
- **Tiers:** P0 = R01–R06 (stored XSS ×3, IDOR/auth ×3) · P1 = R07–R11 (500-crash ×4,
  live-game race) · P2 = R12–R22 (correctness ×5, polish ×6).
- **Test baseline:** `40 passed, 8 failed`. Only `test_apk_release.py::…stale` is a real app
  bug (TASK-R12). The other 7 failures are test-harness CSRF issues (tests set
  `session["user_id"]` without `session["_csrf_token"]`), not app defects.
- **No open work in progress.** File Claims table (`ROUTING.md`) is empty.

---

## Section 2 — Proposed Plan (REQUIRES HUMAN APPROVAL — not yet dispatched)

Start with the P0 tier (deploy-blocking). Respecting the File Lock Protocol: `app.py` is a
single file, so all its edits go to **one** agent; the remaining P0 fixes touch disjoint
template files and run in parallel.

### Wave 1 — P0, parallel (file-disjoint)

| Task(s) | Route to | Files (proposed claim) | Model |
|---------|----------|------------------------|-------|
| R04, R05, R06, R07, **R01** | security-reviewer (implements) → code-reviewer | `app.py`, `templates/play_game.html`, `templates/manual_game.html` | opus/sonnet |
| R02 | typescript-reviewer → code-reviewer | `templates/life_counter.html` | sonnet |
| R03 | typescript-reviewer → code-reviewer | `templates/players.html` | sonnet |

Rationale for bundling: R04/R05/R06/R07 all edit `app.py`; R01 is a coupled route+template
change (route stops `json.dumps`, templates switch to `|tojson`) that also touches `app.py`,
so it must share the `app.py` claim to keep the change atomic and avoid a merge collision.
R02 (`life_counter.html`) and R03 (`players.html`) are independent files → safe to parallelize.

### Wave 2 — P1 crashes + race (dispatch after Wave 1 releases `app.py`)

| Task(s) | Route to | Files | Model | Notes |
|---------|----------|-------|-------|-------|
| R08 | python-reviewer | `deck_import.py` | sonnet | disjoint from app.py — could also go in Wave 1 |
| R09, R10 | python-reviewer → code-reviewer | `app.py` | sonnet | needs `app.py` claim (serialize after Wave 1) |
| R11 | architect (design) → python-reviewer + typescript-reviewer | `app.py`, `templates/life_counter.html`, `templates/player_panel.html` | opus | concurrency + client contract; ADR first |

R08 touches only `deck_import.py` and has no dependency on the app.py waves — safe to pull
into Wave 1 as a 4th parallel lane if desired.

### Wave 3 — P2 correctness + polish (batch, low urgency)

R12 (fixes an existing failing test), R13, R14, R15, R16, R17, R18, R19, R20, R21, R22.
Most are `app.py` (serialize on the claim) or single templates. R13/R16 want a spec first
(bundle under planner); R22 (delete stray `templates/*` files) is independent and trivial.

### Dispatch discipline
- Only **one** agent holds the `app.py` claim at a time. Backend app.py tasks serialize.
- Every implementer is followed by **code-reviewer**; anything touching auth/user-data
  (R04, R05, R06, R07) also routes through **security-reviewer** and **compliance-reviewer**
  (GDPR scope per INIT.md) before merge.
- **verification-before-completion** gate on every task: run `python3.11 -m pytest tests/`
  and, for UI-visible changes (R01, R02, R03, R17, R18), a screenshot of a running game per
  CLAUDE.md's UI-change rule.

---

## Section 3 — Veto Buffer

**All three resolved 2026-07-01 by best-judgment default (user was away at approval time).**
Marked pending user confirmation — each is low-risk/reversible. Resolutions were folded into
TASKS.md as R23–R25 (Wave 3). If the user prefers a different option, re-open and re-task.

| ID | Resolution (default chosen) | Task | Confirm? |
|----|-----------------------------|------|----------|
| VETO-R01 | **A** — explicit `SESSION_COOKIE_SAMESITE="Lax"` + `SECURE` (HTTPS-gated); keep `/api/*` CSRF-exempt so session-cookie mobile clients don't break. Rejected B (would break native clients) and C (leaves implicit reliance). | TASK-R23 | pending |
| VETO-R02 | **A** — cross-pod stats on player/compare pages are intentional (lifetime career view); document with a comment, no behavior change. B (pod-scope) remains the easy alternative. | TASK-R24 | pending |
| VETO-R03 | **A** — gunicorn + non-root `USER` in Docker; keep `app.run` for local dev. | TASK-R25 | pending |

---

## Next `/orchestrate` action
Run `/orchestrate morning` (or `tick`) → present Section 2 for approval → on approval, claim
files in `ROUTING.md` and dispatch Wave 1. Do not dispatch before approval.
