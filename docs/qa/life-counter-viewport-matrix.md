# Life Counter Viewport Test Matrix

Date: 2026-02-25
Target screen: `/life_counter` with 4 active players in a running game session.

## Matrix profiles

| Profile | Viewport (w×h) | Device intent |
|---|---:|---|
| Small phone | 320×568 | compact legacy/small handset |
| Standard phone | 390×844 | modern phone baseline |
| Notched phone | 393×852 | notch-class phone baseline |
| Tablet portrait | 768×1024 | 8–10" tablet portrait |
| Tablet landscape | 1024×768 | 8–10" tablet landscape |

## Verification criteria per profile

- **Clipping:** no life card clipped outside viewport.
- **Overlap:** no life-card overlap with neighboring cards.
- **Tap reliability:** life total updates reliably when tapping left/right zones on a player card.
- **Center-button interference:** center pass button must not block card interactions and should respond to tap.

## Interaction invariants (acceptance)

- Every seat must expose two repeatable tap zones on the life card that map to opposite life deltas (`+1` vs `-1`) for that seat orientation.
- The center pass control must be clickable/tappable for turn advance.
- Outside the visible circular center control bounds, adjacent card zones must remain tappable (no invisible blocking overlay).

## Debug overlay mode for UI regression triage

Use the query parameter `debug_ui=1` while running in Flask debug mode to render visual overlays on the life counter page:

- **Card boundaries:** cyan outlines around each `.life-card`.
- **Interactive hit boxes:** red outlines around buttons/tap targets (including the center pass hit area).
- **Safe-area inset boundaries:** yellow dashed frame showing `safe-area-inset-*` limits.

Example URL:

- `http://localhost:5000/life_counter?debug_ui=1`

Notes:

- Overlay rendering is guarded to development-only (`app.debug`) and does not activate in production deployments.
- Keep at least one screenshot from an active game session with overlays enabled when diagnosing UI layout/tap-regression issues.

## Run checklist/results

| Profile | Clipping | Overlap | Tap reliability | Center-button interference | Notes |
|---|---|---|---|---|---|
| Small phone | ✅ Pass | ✅ Pass | ❌ Fail | ✅ Pass | Center pass now advances turns and diagonal probes around the hit circle reach card zones; top-seat left/right taps still collapse to same delta on compact portrait rotation and need follow-up. |
| Standard phone | ✅ Pass | ✅ Pass | ❌ Fail | ✅ Pass | Same remaining tap-direction issue as small phone; center control no longer blocks adjacent card taps. |
| Notched phone | ✅ Pass | ✅ Pass | ❌ Fail | ✅ Pass | Same remaining tap-direction issue as small phone; center control interaction is non-blocking outside visual bounds. |
| Tablet portrait | ✅ Pass | ✅ Pass | ✅ Pass | ✅ Pass | Both life tap regions and center pass interaction pass. |
| Tablet landscape | ✅ Pass | ✅ Pass | ✅ Pass | ✅ Pass | Both life tap regions and center pass interaction pass. |

## Seed/setup used for this run

- Added test players: `QA Matrix Player 1..4`.
- Added 2 decks per player (`QA Matrix <Player> Deck 1/2`) with non-empty pasted decklist text.
- Started a 4-player game from `/play_game` and navigated to active `/life_counter`.

## Screenshot artifacts (latest run)

- Small phone: ![Small phone life counter](browser:/tmp/codex_browser_invocations/d826417103a0517d/artifacts/artifacts/life-counter-small_phone.png)
- Standard phone: ![Standard phone life counter](browser:/tmp/codex_browser_invocations/d826417103a0517d/artifacts/artifacts/life-counter-standard_phone.png)
- Notched phone: ![Notched phone life counter](browser:/tmp/codex_browser_invocations/d826417103a0517d/artifacts/artifacts/life-counter-notched_phone.png)
- Tablet portrait: ![Tablet portrait life counter](browser:/tmp/codex_browser_invocations/d826417103a0517d/artifacts/artifacts/life-counter-tablet_portrait.png)
- Tablet landscape: ![Tablet landscape life counter](browser:/tmp/codex_browser_invocations/d826417103a0517d/artifacts/artifacts/life-counter-tablet_landscape.png)
