#!/usr/bin/env python3
"""Refresh stored decklists; dry-run by default. Uses the app's configured DB.

Run in the intended channel container after backing up that database:
  python scripts/refresh_deck_tags.py
  python scripts/refresh_deck_tags.py --apply
An optional --card-cache file contains a name -> Scryfall card JSON mapping,
allowing the exact reviewed card snapshot to be reused across Test and Prod.
Decklists and manual strategy overrides are preserved. No list means no inference.
"""
import argparse
import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--card-cache", type=Path)
    parser.add_argument("--deck-id", type=int, action="append")
    args = parser.parse_args()
    with app.app.app_context():
        query = app.Deck.query.order_by(app.Deck.id)
        if args.deck_id:
            query = query.filter(app.Deck.id.in_(args.deck_id))
        snapshots = []
        skipped = 0
        for deck in query.all():
            if not (deck.decklist_text or "").strip():
                skipped += 1
                continue
            parsed = app.parse_plaintext_decklist(deck.decklist_text).as_dict()
            snapshots.append((deck.id, deck.name, deck.decklist_text, deck.tags_json,
                              app.extract_decklist_card_names(parsed)))
        app.db.session.rollback()  # Release the read transaction before lookups.
        if args.card_cache:
            raw = json.loads(args.card_cache.read_text())
            if not isinstance(raw, dict) or any(not isinstance(v, dict) for v in raw.values()):
                parser.error("Card cache must map names to card objects")
            cache = {name.lower(): value for name, value in raw.items()}
        else:
            names = sorted({name for row in snapshots for name in row[4]})
            cache = {}
            for start in range(0, len(names), 75):
                cache.update(app.scryfall_collection(names[start:start + 75]))
                time.sleep(0.1)
        results = []
        for deck_id, name, text, old_tags, names in snapshots:
            tags, diagnostic = app.compute_deck_tags(names, card_cache=cache)
            target = SimpleNamespace(decklist_text=text, tags_json=old_tags)
            app.apply_deck_tags(target, tags, diagnostic, preserve_existing=True)
            summary = app.get_deck_tag_summary(target)
            print(json.dumps({"id": deck_id, "name": name, "status": summary["status"],
                              "mechanics": [key for key, value in tags.items() if value],
                              "strategies": [row["label"] for row in summary["strategies"]],
                              "unresolved": summary["unresolved_cards"]}), flush=True)
            results.append((deck_id, text, old_tags, target))
        if args.apply:
            # Refuse to overwrite concurrent list changes or manual corrections.
            from sqlalchemy import update
            try:
                for deck_id, text, old_tags, target in results:
                    result = app.db.session.execute(
                        update(app.Deck).where(app.Deck.id == deck_id,
                                               app.Deck.decklist_text == text,
                                               app.Deck.tags_json == old_tags).values(
                            tags_json=target.tags_json, tags_version=target.tags_version,
                            tags_computed_at=target.tags_computed_at))
                    if result.rowcount != 1:
                        raise RuntimeError(f"Deck {deck_id} changed during refresh; no results were saved. Retry.")
                app.db.session.commit()
            except Exception:
                app.db.session.rollback()
                raise
        print(f"{'Updated' if args.apply else 'Previewed'} {len(results)} decks; skipped {skipped} without lists.")


if __name__ == "__main__":
    main()
