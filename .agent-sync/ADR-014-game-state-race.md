# ADR-014: Eliminating the lost-update race in the live game-state polling API

## Status
Proposed — 2026-07-02 (TASK-R11)

## Context
`api_game_state` POST (`app.py:7016-7177`) does an unsynchronised read-modify-write of the
whole `ActiveGame.state_json` blob (read `:7058`, per-key merge `:7062-7170`, version bump
`:7172`, write+commit `:7173-7175`). No row lock, no base-version check. Concurrent POSTs from
4 devices → second commit overwrites the first → dropped life/flag/counter updates.

Two code facts shape the fix:
- The merge is **already per-key/surgical** — only keys the client sent are written. The bug is
  **atomicity** of the RMW, not the merge.
- Clients treat `version` as a monotonic "newer state" signal and send **no base version**
  (`life_counter.html:2101`, `player_panel.html:240`). Host pushes life as an **absolute**;
  phone pushes life as a **delta** (`player_panel.html:148`).

Deployment: SQLite (`app.py:59`, no engine opts), Flask threaded dev server today, **gunicorn
`-w 4` planned (R25)**. `SELECT ... FOR UPDATE` is a no-op on SQLite; the only cross-process
serialisation primitive is the SQLite write lock. An in-process `threading.Lock` would NOT span
4 gunicorn workers — so it silently stops fixing the bug once R25 lands.

## Decision
**Option B2: wrap the read-modify-write in a SQLite `BEGIN IMMEDIATE` write transaction with a
`busy_timeout`, and enable WAL.** Keep the per-key merge verbatim; keep `version` as a monotonic
signal (not a write precondition). **No client-contract change** — both templates and deployed
mobile companions keep working byte-for-byte.

Rejected: (A) optimistic version check — changes both client files + breaks in-field mobile
clients, and is *less* correct for delta-pushed life while adding churn; (B1) in-process lock —
doesn't span gunicorn workers; (C) `json_set()` field UPDATEs — can't express the nested
card_state/pass_turn merges, over-engineered.

## Consequences
- **app.py:** add `SQLALCHEMY_ENGINE_OPTIONS={"connect_args":{"timeout": SQLITE_BUSY_TIMEOUT_SECONDS}}`
  (constant, e.g. 15s); a `connect` PRAGMA listener (`journal_mode=WAL`, `synchronous=NORMAL`,
  `isolation_level=None`); in the POST handler, hold `BEGIN IMMEDIATE` **before** the authoritative
  re-read, run the unchanged merge, write, commit; on `OperationalError` (busy timeout) rollback +
  log + return `503` (retryable), never silent.
- **Clients:** no change. Only new visible response is an occasional `503` under extreme
  contention, which both clients already treat as transient and back off on.
- **Migration:** WAL adds `-wal`/`-shm` sidecars in `data/` (gitignored). No schema change.
- **Test:** RED — two concurrent POSTs (A: life_delta, B: flag) from same base version; assert both
  survive (fails today). GREEN after fix; add same-key delta-sum test. unittest style, matching
  `tests/test_api_game_state.py`; test DB URI must carry the timeout/WAL options.

## Risk to watch (orchestrator note)
This is a **global DB-engine change** (WAL + manual transaction control via raw connection while
the rest of the app uses the SQLAlchemy ORM). The implementer must verify the raw
`BEGIN IMMEDIATE` / `db.session.commit()` interplay doesn't corrupt ORM session state on other
routes, and that the `connect` listener binds correctly given app/db init order. Rigorous review +
the concurrency test are mandatory before merge.

Full ADR (options analysis + implementation sketch) is in the R11 architect agent transcript.
