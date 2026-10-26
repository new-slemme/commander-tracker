#!/usr/bin/env python3
"""Add MP4/poster derivatives to stored commander GIFs. Originals stay intact.

Run with the same COMMANDER_DB_URI / COMMANDER_ART_DIR / storage environment as
the app. Use --dry-run to inventory; --all-local includes unreferenced local GIFs.
"""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--all-local", action="store_true")
    args = parser.parse_args()
    import app

    failures = 0
    with app.app.app_context():
        sources = {d.commander_art_url for d in app.Deck.query.all()
                   if d.commander_art_url and d.commander_art_url.lower().endswith(".gif")}
        if args.all_local:
            sources.update("/art/" + str(p.relative_to(app.ART_DIR))
                           for p in app.ART_DIR.rglob("*") if p.suffix.lower() == ".gif")
        for source in sorted(sources):
            try:
                if not source.startswith(("/art/", "/media/custom-art/")):
                    result = {"status": "skipped", "reason": "external URL; upload to convert"}
                elif args.dry_run:
                    result = {"status": "existing" if app.get_variants(app.ART_DIR, source) else "pending"}
                else:
                    result = app.convert_legacy_commander_art(source)
                print(json.dumps({"source": source, **result}), flush=True)
            except Exception as exc:
                failures += 1
                print(json.dumps({"source": source, "status": "failed", "error": str(exc)}), flush=True)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
