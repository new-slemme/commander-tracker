# AGENTS.md

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
