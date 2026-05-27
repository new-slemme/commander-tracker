# MMR Implementation

This document describes the MMR (Match Making Rating) system added to the Commander Tracker web app. It is intended as a reference for porting to the Android companion app (`edh-son-android`).

---

## Overview

Each **deck** (not player) carries an integer MMR rating. After every game, MMR is redistributed between all participating decks based on who won and how strong each deck was expected to be. Winning against stronger opponents earns more MMR than winning against weaker ones.

---

## Constants

```
STARTING_MMR = 1000    # Default MMR for every new or existing deck
K_FACTOR     = 48      # Maximum points exchanged per game
MMR_FLOOR    = 100     # A deck's MMR can never drop below this
```

---

## Formula

### Expected win probability (proportional share)

```
expectedWin(deck_i) = mmr_i / Σ(all pod MMRs)
```

Example — four decks at 1000/1000/1000/1000:
- Each expected win = 1000 / 4000 = 0.25 (25%)

Example — four decks at 1200/1000/950/850 (total 4000):
- Deck A (1200): 1200/4000 = 30%
- Deck B (1000): 1000/4000 = 25%
- Deck C (950): 950/4000 = 23.75%
- Deck D (850): 850/4000 = 21.25%

### MMR delta per deck

```
delta(deck_i) = round(K × (actual_i − expected_i))
```

Where `actual_i = 1` for the winning deck and `0` for all other decks.

The sum of all deltas is always approximately 0 (zero-sum, with rounding up to ±N across N players).

### MMR floor

```
new_mmr = max(MMR_FLOOR, current_mmr + delta)
```

### Example — four equal decks

```
delta(winner) = round(48 × (1 − 0.25)) = round(36.0)  = +36
delta(loser)  = round(48 × (0 − 0.25)) = round(-12.0) = -12
```

### Example — low-MMR upset

Pod: 1200, 1000, 950, 850  
Deck D (850) wins:

```
expectedD = 850 / 4000 = 0.2125
delta(D)  = round(48 × (1 − 0.2125)) = round(37.8) = +38
delta(A)  = round(48 × (0 − 0.30))   = -14
delta(B)  = round(48 × (0 − 0.25))   = -12
delta(C)  = round(48 × (0 − 0.2375)) = -11
```

---

## Tier System

| Tier | Threshold |
|------|-----------|
| S    | ≥ 1300    |
| A    | ≥ 1200    |
| B    | ≥ 1100    |
| C    | ≥ 950     |
| D    | < 950     |

---

## Database Schema

Three columns were added across existing tables.

### `deck` table

| Column             | Type                           | Default |
|--------------------|--------------------------------|---------|
| `mmr`              | `INTEGER NOT NULL`             | `1000`  |
| `mmr_history_json` | `TEXT NOT NULL`                | `'[]'`  |

`mmr_history_json` is a JSON array of entries added after each game:
```json
[
  {
    "game_id": 42,
    "delta": 36,
    "mmr_after": 1036,
    "date": "2025-06-01T20:30:00"
  }
]
```

### `game` table

| Column            | Type          | Default |
|-------------------|---------------|---------|
| `mmr_deltas_json` | `TEXT`        | `NULL`  |

Stores the deltas for all participants in one game:
```json
[
  {"deck_id": 3, "delta": 36},
  {"deck_id": 7, "delta": -12},
  {"deck_id": 9, "delta": -12},
  {"deck_id": 11, "delta": -12}
]
```

### `game_participant` table

| Column      | Type      | Default |
|-------------|-----------|---------|
| `mmr_delta` | `INTEGER` | `NULL`  |

Denormalized per-participant delta for efficient per-deck history queries.

---

## Core Python Functions

Located near the top of `app.py`, above the model definitions.

```python
STARTING_MMR = 1000
K_FACTOR = 48
MMR_FLOOR = 100

def mmr_tier(mmr: int) -> str:
    if mmr >= 1300: return "S"
    if mmr >= 1200: return "A"
    if mmr >= 1100: return "B"
    if mmr >= 950:  return "C"
    return "D"

def calculate_expected_wins(deck_mmrs: list[int]) -> list[float]:
    total = sum(deck_mmrs)
    return [mmr / total for mmr in deck_mmrs]

def calculate_mmr_deltas(deck_mmrs: list[int], winner_index: int) -> list[int]:
    expected = calculate_expected_wins(deck_mmrs)
    return [
        round(K_FACTOR * ((1 if i == winner_index else 0) - expected[i]))
        for i in range(len(deck_mmrs))
    ]
```

---

## Game Logging Integration

MMR is updated atomically in the same database transaction as the game record. This happens in two routes:

- `end_game` (POST `/end_game`) — normal live game flow
- `manual_record_game` (POST `/record_game`) — manual entry

**Pattern (runs before `db.session.commit()`):**

```python
pod_deck_ids = [p["deck_id"] for p in participants]
winner_deck_id = next((p["deck_id"] for p in participants if p["player_id"] == winner_id), None)

if winner_deck_id is not None and len(pod_deck_ids) >= 2:
    winner_index = pod_deck_ids.index(winner_deck_id)
    pod_decks = [db.session.get(Deck, did) for did in pod_deck_ids]
    pod_mmrs = [d.mmr for d in pod_decks]
    mmr_deltas = calculate_mmr_deltas(pod_mmrs, winner_index)

    for deck, delta in zip(pod_decks, mmr_deltas):
        new_mmr = max(MMR_FLOOR, deck.mmr + delta)
        history = json.loads(deck.mmr_history_json or "[]")
        history.append({
            "game_id": game.id,
            "delta": delta,
            "mmr_after": new_mmr,
            "date": datetime.utcnow().isoformat()
        })
        deck.mmr = new_mmr
        deck.mmr_history_json = json.dumps(history)

    game.mmr_deltas_json = json.dumps([
        {"deck_id": did, "delta": d}
        for did, d in zip(pod_deck_ids, mmr_deltas)
    ])
```

The `GameParticipant` objects are created in the same loop with `mmr_delta=mmr_deltas[i]`.

---

## API Exposure

The REST API serializes decks via `_serialize_deck_summary()`. MMR fields are included:

```json
{
  "id": 3,
  "name": "Atraxa Superfriends",
  "commander": "Atraxa, Praetors' Voice",
  "mmr": 1147,
  "mmr_tier": "B",
  ...
}
```

Relevant REST endpoints:
- `GET /api/decks` — list all decks, each includes `mmr` and `mmr_tier`
- `GET /api/decks/<id>` — single deck detail

Game history per deck is not yet a dedicated REST endpoint but is derivable from `game_participant.mmr_delta` joined to games.

---

## Android Integration Notes

### Displaying MMR in deck selection

When showing the deck picker before a game, fetch deck list from `GET /api/decks`. Each deck entry includes:
- `mmr` (integer) — current rating
- `mmr_tier` (string, one of `S/A/B/C/D`) — display as a badge

### Expected win preview

When the user has selected all decks for a pod, compute locally:

```kotlin
fun expectedWin(deckMmr: Int, allMmrs: List<Int>): Double {
    return deckMmr.toDouble() / allMmrs.sum()
}
```

Display as "Expected win: 28%" next to each deck in the game setup screen.

### Post-game delta display

After a game is submitted (via `POST /api/games` or the existing end-game flow), the response (or a follow-up `GET /api/games/<id>`) will include `mmr_deltas_json` on the game record. Parse and display as `+36` (green) or `-12` (red) next to each deck.

### MMR history chart

Deck detail screen can render an MMR progression line chart using `mmr_history_json` from `GET /api/decks/<id>`. Each entry has `mmr_after` and `date` for the x/y axes.

### Tier badge colors (suggestion)

| Tier | Color       |
|------|-------------|
| S    | Gold `#FFD700` |
| A    | Purple `#9B59B6` |
| B    | Blue `#3498DB` |
| C    | Green `#27AE60` |
| D    | Gray `#7F8C8D` |
