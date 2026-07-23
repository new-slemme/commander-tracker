#!/usr/bin/env python3
"""WAL-safe, rotated backup for commander.db.

The old cron did a plain ``cp commander.db commander.db.cron_backup`` — which is
UNSAFE now that the DB runs in WAL mode (R11): a bare file copy misses data still
in the ``-wal`` file and can capture a torn write. It also overwrote a single
file, so there was no history.

This uses SQLite's online backup API (equivalent to ``.backup``), which produces
a consistent snapshot across WAL, and keeps dated, rotated copies.

Usage:
    python3 scripts/backup_db.py                 # backs up ./data/commander.db
    COMMANDER_DB_FILE=/data/commander.db python3 scripts/backup_db.py
    BACKUP_KEEP=96 python3 scripts/backup_db.py

Cron (every 30 min), from the repo root:
    */30 * * * * cd /home/slemme/mine/commander-tracker && /usr/bin/python3 scripts/backup_db.py >> data/backups/backup.log 2>&1
"""

import datetime
import os
import sqlite3
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_FILE = os.getenv("COMMANDER_DB_FILE", os.path.join(REPO_ROOT, "data", "commander.db"))
BACKUP_KEEP = int(os.getenv("BACKUP_KEEP", "96"))  # e.g. 96 = 48h at 30-min cadence


def main() -> int:
    if not os.path.exists(DB_FILE):
        print(f"[backup] source DB not found: {DB_FILE}", file=sys.stderr)
        return 1

    backups_dir = os.path.join(os.path.dirname(DB_FILE), "backups")
    os.makedirs(backups_dir, exist_ok=True)
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d-%H%M%SZ")
    dest = os.path.join(backups_dir, f"commander-{stamp}.db")

    src = sqlite3.connect(f"file:{DB_FILE}?mode=ro", uri=True)
    try:
        dst = sqlite3.connect(dest)
        try:
            src.backup(dst)  # consistent snapshot across WAL
        finally:
            dst.close()
        # verify the snapshot opens and passes integrity_check
        check = sqlite3.connect(f"file:{dest}?mode=ro", uri=True)
        try:
            ok = check.execute("PRAGMA integrity_check").fetchone()[0]
        finally:
            check.close()
        if ok != "ok":
            print(f"[backup] integrity check FAILED on {dest}: {ok}", file=sys.stderr)
            return 2
    finally:
        src.close()

    # rotate: keep the newest BACKUP_KEEP snapshots
    snaps = sorted(
        (os.path.join(backups_dir, f) for f in os.listdir(backups_dir)
         if f.startswith("commander-") and f.endswith(".db")),
        key=os.path.getmtime,
        reverse=True,
    )
    removed = 0
    for stale in snaps[BACKUP_KEEP:]:
        try:
            os.remove(stale)
            removed += 1
        except OSError:
            pass

    print(f"[backup] {datetime.datetime.now(datetime.timezone.utc).isoformat()} -> {dest} "
          f"(kept {min(len(snaps), BACKUP_KEEP)}, pruned {removed})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
