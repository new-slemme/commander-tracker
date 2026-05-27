# MMR System Implementation Guide
**For:** AI Coding Agent  
**Context:** Retrofitting a Multiplayer Elo MMR system into an existing MTG Commander game tracker

---

## Overview

This guide tells you exactly what to add, change, and preserve when integrating an MMR rating system into an existing Commander tracker. The owner currently ranks decks by flat win rate percentage. The new system replaces (or supplements) that with a **Multiplayer Elo** rating that rewards beating stronger opponents more than beating weaker ones.

Read this entire document before touching any code.

---

## 1. Core Concept — Multiplayer Elo

### How it works

Every deck has a numeric MMR rating. When a game is logged, each deck's **expected win probability** is calculated proportionally to its share of the total pod MMR. The winner gains MMR; the losers lose MMR. The bigger the upset, the bigger the swing.

### The formula

```
expectedWin(deck_i) = mmr_i / (mmr_1 + mmr_2 + mmr_3 + mmr_4)

delta(deck_i) = K × (actualScore_i − expectedWin_i)

  where actualScore = 1 for the winner, 0 for all losers
```

### Constants

| Constant | Value | Notes |
|---|---|---|
| `STARTING_MMR` | `1000` | Assigned to every new deck |
| `K_FACTOR` | `48` | Controls volatility. Higher = faster movement. 48 suits small sample sizes. |
| `MMR_FLOOR` | `100` | A deck's MMR can never drop below this. |

### Example

Pod MMRs: 1200, 1000, 950, 850. Total = 4000.

Expected win chances: 30%, 25%, 23.75%, 21.25%.

If the 850 MMR deck wins:
- Winner (+): `48 × (1 − 0.2125)` = **+38**
- Loser 1200 (−): `48 × (0 − 0.30)` = **−14**
- Loser 1000 (−): `48 × (0 − 0.25)` = **−12**
- Loser 950 (−): `48 × (0 − 0.2375)` = **−11**

If the 1200 MMR deck wins:
- Winner (+): `48 × (1 − 0.30)` = **+34**
- Same losers drop by more (they were "expected" to do better).

---

## 2. Data Model Changes

### What to add to each deck record

The existing deck records need two new fields. Do **not** remove or rename existing fields — only append.

```js
// Before (existing shape — field names may differ in the codebase)
{
  id: "...",
  player: "Alice",
  deck: "Atraxa",
  wins: 4,
  games: 10
}

// After — add exactly these two fields
{
  id: "...",
  player: "Alice",
  deck: "Atraxa",
  wins: 4,
  games: 10,
  mmr: 1000,        // number — current MMR rating
  mmrHistory: []    // array — log of every delta for sparkline/history use
}
```

`mmrHistory` entries look like:

```js
{ gameId: "abc123", delta: +38, mmrAfter: 1038, date: "2025-05-27T..." }
```

### What to add to each game record

```js
// Before
{
  id: "...",
  date: "...",
  players: [...],
  winner: "deckId"
}

// After — add this field
{
  id: "...",
  date: "...",
  players: [...],
  winner: "deckId",
  mmrDeltas: [
    { deckId: "id1", delta: +38 },
    { deckId: "id2", delta: -14 },
    { deckId: "id3", delta: -12 },
    { deckId: "id4", delta: -11 }
  ]
}
```

### Migration for existing decks

When the app loads, check every deck. If `mmr` is `undefined`, set it to `1000` and set `mmrHistory` to `[]`. Do this once at startup before any other logic runs. Do **not** back-calculate MMR from existing game history — start fresh from `1000` for all existing decks.

```js
function migrateDeck(deck) {
  if (deck.mmr === undefined) deck.mmr = 1000;
  if (!deck.mmrHistory) deck.mmrHistory = [];
  return deck;
}
```

---

## 3. New Functions to Add

Add these as pure utility functions, separate from any UI or storage code. They have no side effects.

### `calculateExpectedWins(deckMmrs)`

```js
/**
 * Returns expected win probability for each deck in the pod.
 * @param {number[]} deckMmrs - Array of 4 MMR values
 * @returns {number[]} - Array of 4 probabilities summing to 1.0
 */
function calculateExpectedWins(deckMmrs) {
  const total = deckMmrs.reduce((sum, mmr) => sum + mmr, 0);
  return deckMmrs.map((mmr) => mmr / total);
}
```

### `calculateMmrDeltas(deckMmrs, winnerIndex)`

```js
/**
 * Returns the MMR delta (positive or negative integer) for each deck.
 * @param {number[]} deckMmrs   - Array of 4 MMR values in pod seat order
 * @param {number}   winnerIndex - 0-based index of the winning deck
 * @returns {number[]} - Array of 4 integer deltas
 */
function calculateMmrDeltas(deckMmrs, winnerIndex) {
  const K = 48;
  const expected = calculateExpectedWins(deckMmrs);
  return deckMmrs.map((_, i) => {
    const actual = i === winnerIndex ? 1 : 0;
    return Math.round(K * (actual - expected[i]));
  });
}
```

### `applyMmrDeltas(decks, podDeckIds, deltas, gameId)`

```js
/**
 * Returns a NEW array of decks with updated MMR — does not mutate the input.
 * @param {object[]} decks      - Full deck list
 * @param {string[]} podDeckIds - Array of 4 deck IDs in pod seat order
 * @param {number[]} deltas     - Array of 4 deltas from calculateMmrDeltas
 * @param {string}   gameId     - ID of the game being logged
 * @returns {object[]} - Updated deck array
 */
function applyMmrDeltas(decks, podDeckIds, deltas, gameId) {
  const MMR_FLOOR = 100;
  return decks.map((deck) => {
    const seatIndex = podDeckIds.indexOf(deck.id);
    if (seatIndex === -1) return deck; // not in this pod, untouched

    const delta = deltas[seatIndex];
    const newMmr = Math.max(MMR_FLOOR, deck.mmr + delta);
    const historyEntry = {
      gameId,
      delta,
      mmrAfter: newMmr,
      date: new Date().toISOString(),
    };

    return {
      ...deck,
      mmr: newMmr,
      mmrHistory: [...(deck.mmrHistory ?? []), historyEntry],
    };
  });
}
```

---

## 4. Changes to the Game Logging Flow

Find the existing function that handles submitting/saving a game result. It currently updates `wins` and `games` counters. Extend it — do not replace it.

### Step-by-step

1. Collect the 4 deck IDs in pod order and the winner deck ID (this logic already exists).
2. Resolve the current `mmr` value for each of the 4 decks.
3. Call `calculateMmrDeltas` with those 4 MMR values and the winner's index.
4. Call `applyMmrDeltas` to get the updated deck list.
5. Attach `mmrDeltas` to the game record before saving it.
6. Save the updated deck list and game record together (same save call if possible — avoid partial writes).

### Pseudocode patch

```js
// EXISTING logic you are extending (do not remove):
//   - Incrementing deck.wins for winner
//   - Incrementing deck.games for all
//   - Saving the game record

// ADD after resolving podDeckIds and winnerIndex:
const podMmrs = podDeckIds.map((id) => decks.find((d) => d.id === id).mmr);
const deltas  = calculateMmrDeltas(podMmrs, winnerIndex);
const updatedDecks = applyMmrDeltas(decks, podDeckIds, deltas, newGame.id);

// ADD to the game record before saving:
newGame.mmrDeltas = podDeckIds.map((id, i) => ({ deckId: id, delta: deltas[i] }));

// REPLACE the deck save with updatedDecks (it already includes wins/games if
// applyMmrDeltas was written to spread the full deck object, which it is).
// BUT: wins and games are NOT updated inside applyMmrDeltas — it only touches
// mmr and mmrHistory. Keep your existing wins/games update logic separately.
```

> **Important:** `applyMmrDeltas` only modifies `mmr` and `mmrHistory`. Your existing code that increments `wins` and `games` must still run. Apply both sets of changes to the same deck object before saving.

---

## 5. Changes to Existing UI

### Leaderboard / ranking list

- Add an `MMR` column or value display alongside (or replacing) win rate %.
- Sort by `mmr` descending by default. Offer a toggle to sort by win rate if the user wants the old view.
- Optionally display a tier badge based on MMR range:

| Tier | MMR threshold |
|---|---|
| S | ≥ 1300 |
| A | ≥ 1200 |
| B | ≥ 1100 |
| C | ≥ 950 |
| D | < 950 |

### Game log / history view

- For each logged game, display the MMR delta per deck (e.g. `+38`, `−14`).
- Source this from `game.mmrDeltas`, not by recalculating.

### Deck detail view (if one exists)

- Show current MMR prominently.
- If you render `deck.mmrHistory`, you can plot a simple MMR-over-time sparkline.

### Game logging form

- After the user selects all 4 decks but before they confirm, show each deck's current MMR and expected win probability (`expectedWin × 100`%). This gives immediate context on the pod's power balance.
- After selecting a winner, show the projected MMR deltas so the user can see what will happen before committing.

---

## 6. What NOT to Change

- Do not alter the schema of any field that already exists.
- Do not remove win rate % — keep it as a secondary stat.
- Do not recalculate historical games retroactively. MMR starts fresh from the point of integration.
- Do not change game validation logic (pod size, duplicate deck checks, etc.).
- Do not change how `id`, `date`, `player`, `deck`, or `winner` fields are stored.

---

## 7. Testing Checklist

Run through these scenarios manually or in a test suite before shipping:

- [ ] A new deck is created → `mmr` is `1000`, `mmrHistory` is `[]`
- [ ] An existing deck without `mmr` field loads → migration sets it to `1000`
- [ ] Log a game with 4 equal-MMR decks (all 1000) → winner gets `+36`, each loser gets `−12`
- [ ] Log a game where the lowest-MMR deck wins → winner's delta is larger than it would be if the highest-MMR deck won
- [ ] Log a game where the highest-MMR deck wins → winner's delta is smaller, losers drop less
- [ ] A deck at `MMR_FLOOR` (100) loses → MMR stays at `100`, does not go negative
- [ ] The sum of all deltas in a game is approximately `0` (rounding may cause ±1)
- [ ] Game record contains correct `mmrDeltas` array after logging
- [ ] `mmrHistory` on a deck grows by one entry per game played
- [ ] Leaderboard sorts by MMR correctly after several games

---

## 8. Scope Boundaries

This integration touches:

- The deck data model (two new fields)
- The game data model (one new field)
- The game logging function (extend, not rewrite)
- The leaderboard display (add MMR column/sort)
- The game history display (add delta display)
- The deck migration at startup (one guard clause)

It does **not** require:

- Changes to authentication or user management
- Changes to database schema beyond the fields listed above (if using a document store like Firestore or a JSON file, no migration script is needed beyond the in-app guard)
- Any new dependencies or libraries
- Changes to routing or navigation structure
