# Deck tags

Version 3 separates life-counter mechanic flags from strategy suggestions and records lookup completeness and a hash of the saved list. Changing a decklist invalidates its analysis. Sideboards and maybeboards are excluded.

A failed lookup leaves the result incomplete and eligible for retry. Refreshing an unchanged list preserves previously detected positive mechanics during an outage. Successful refreshes replace the automatic results. Imports of changed lists do not inherit old automatic tags.

Strategy suggestions include supporting card names. Most require five distinct supporting cards; artifacts require 12, enchantments 10, equipment 6, and tribal requires 10 creatures of the same type and at least 40% of the creatures in the list. These are explainable suggestions, not definitive archetypes or power ratings. Deck owners and admins can choose Auto, Include, or Exclude per strategy on the deck detail page. Manual choices survive refreshes and decklist removal.

Decks without lists display “Decklist needed”. Lists with fewer than 100 cards display “Partial decklist”; detected tags describe the saved fragment. Neither state is silently treated as a fully analyzed Commander deck.

The existing boolean mechanic keys remain in `tags_json`. Reserved `_analysis` and `_strategy_overrides` fields carry evidence, reliability, and manual choices. Legacy tag exports remain boolean maps. Deck API summaries add `tag_status` and `strategies` (key, label, source, cards, and optional tribe).

## Refresh existing decks

Deploy and verify on Test before promoting to Prod. Back up the selected channel database first. Run inside that channel's container:

```sh
python scripts/refresh_deck_tags.py                 # preview only
python scripts/refresh_deck_tags.py --apply         # save reviewed results
python scripts/refresh_deck_tags.py --deck-id 32    # one deck
```

`--card-cache /tmp/cards.json` accepts a name-to-card JSON mapping to reuse the same reviewed Scryfall responses across environments. Without it, requests are batched in groups of 75. The refresh preserves decklists and manual overrides, skips decks without lists, and aborts the write transaction if any target list or tag record changed during analysis. It never guesses from deck names.

## Test database refresh

Stop only the Test service. Use SQLite's online backup API to retain a dated backup of Test and take a consistent Prod snapshot, including committed WAL data. Restore that snapshot into Test with the backup API, verify integrity and table counts, then restart Test. Do not copy a live SQLite database with `cp` or mirror Test back into Prod. Both local Compose channels share the artwork directory, so restoring the database also restores the Prod artwork references without moving or deleting art files.
