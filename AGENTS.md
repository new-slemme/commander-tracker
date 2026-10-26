# AGENTS.md

## Repository relationship and required workflow

- `~/mine/commander-tracker` is the production server/web checkout (`main`, port 5000).
- `~/mine/commander-tracker-test` is the default development and validation checkout (`test`, port 5001). These two directories are worktrees of the same Git repository.
- `~/mine/edh-son-android` is the separate Android client repository. Its backend is Commander Tracker; backend changes required by Android belong in `commander-tracker-test` first.
- The session's initial working directory is not authorization to develop in production. Use an explicit working directory for edits, builds, and tests.

Before changing code:

1. Read the target checkout's instructions. Inspect `git worktree list`, the branch/HEAD, and `git status --short --branch` for both tracker worktrees (and Android when involved).
2. Compare committed history AND relevant working-tree files, including untracked source files. APK-only release commits can put `main` ahead while server changes remain uncommitted or differ between worktrees; commit counts alone do not establish application parity.
3. Implement server/web/API/schema changes in `commander-tracker-test`. Preserve existing changes in both worktrees. If production has unique work needed by the task, reconcile that work into test with review and validation before continuing dependent development; do not overwrite either checkout wholesale.
4. Run Docker Compose from `~/mine/commander-tracker` for its deployment configuration, but verify the resolved test build context points to `~/mine/commander-tracker-test`. Running Compose from production does not make production the code-editing target. Rebuild the test image after code changes before validating it.

Promotion and Android releases:

- Promote validated server changes from test to production when deployment is authorized. Check both dirty worktrees first, preserve unrelated work, and use reviewed commits/merges rather than copying whole source trees.
- Android APK publication to production's `apk/` directory is a separate release operation. It does not authorize editing production server code or committing unrelated server changes. Follow Android's signing, versioning, and manifest instructions.
- When an Android release needs backend changes, implement and validate them in test, then promote them as part of the authorized release before publishing a client that requires them.
- Bring production release commits back into `test` as part of completing an authorized release/promotion, preserving pending work. Verify both branch ancestry and relevant file contents afterward. If reconciliation remains incomplete, report the exact remaining differences; do not claim the repositories are synchronized.
- Keep test fixtures and verification writes isolated from the live production database. Never overwrite real user data to reconcile code.

## Purpose
Use this guide when preparing test data for the **life counter** workflow.

## Goal state for life-counter testing
Before validating life-counter behavior, make sure the app has:

1. At least **3 active players**. (currently the most frequent real-life use case)
2. At least **1 deck per player** (preferably 2+ so deck selection can be tested).
3. Decks with a real decklist (imported from URL or pasted list), not only a name.
4. A game that includes all players/decks so `/play_game` and life-counter flows are fully exercisable.

## Required setup flow

### 1) Ensure players exist
- Open the Players page and create enough players for a multiplayer pod (4+ recommended).
- Use clear names (example: `Player 1`, `Player 2`, `Player 3`, `Player 4`) so game setup is unambiguous.

### 2) Add decks for each player
- Go to Decks and create decks for every test player.
- Every deck should include a **deck list**. Preferred method:
  - Use deck import and paste an Archidekt URL.
- Canonical import source for seed data:
  - `https://archidekt.com/decks/10697552/pirates_unmodified`

If import is unavailable, paste a plain-text deck list manually (one card per line with quantity) so the deck still has real content.

### 3) Build a test game for the life counter
- Create a game with the prepared players and assigned decks.
- Verify each participant has a distinct player+deck assignment.
- Start the game and confirm the life counter page loads with all participants.

### 4) Run a live game session before in-game UI validation
- Start the web app locally before testing in-game UI behavior (life changes, commander damage, turn flow, etc.).
- Ensure the server binds to `0.0.0.0` so browser automation can connect.
- Confirm the app is reachable from the browser container at `http://localhost:<port>`.
- Navigate through game setup until you are on the active life counter/game screen, not just pre-game forms.

### 5) Screenshot requirement for in-game UI changes
- If a task changes visible in-game UI, capture at least one screenshot of a **running game session** showing all active players.
- Prefer a life counter screen that clearly includes player names, life totals, and game controls impacted by the change.
- Save the screenshot artifact and include a markdown image link in the final report.
- If screenshot capture fails, document the exact failure reason and any connectivity/port mismatch observed.

## Verification checklist (static + UI)
- [ ] Players are present and selectable.
- [ ] Decks exist for each player.
- [ ] Deck records contain imported/pasted decklist text.
- [ ] A multiplayer game can be created using those decks.
- [ ] Life counter initializes with all players and expected starting life.
- [ ] App server is running and reachable from browser automation.
- [ ] At least one screenshot from an active game session is captured for UI-affecting changes.

## Notes for future agents
- Prefer repeatable, deterministic seed data names.
- Do not delete existing real user data; add clearly marked test records instead.
- When adding more sample decks, keep at least one Archidekt-imported list per player to exercise importer paths.
