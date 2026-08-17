#!/usr/bin/env bash
set -euo pipefail

: "${RESTORE_DB_URI:?Set RESTORE_DB_URI to an empty PostgreSQL restore target}"
: "${BACKUP_ENCRYPTION_KEY:?Set BACKUP_ENCRYPTION_KEY used for the backup}"
backup_file="${1:?Usage: restore_postgres.sh /path/to/commander.dump.enc}"

test -f "$backup_file"
openssl enc -d -aes-256-cbc -pbkdf2 -pass env:BACKUP_ENCRYPTION_KEY -in "$backup_file" \
  | pg_restore --dbname="$RESTORE_DB_URI" --clean --if-exists --no-owner --no-acl

psql "$RESTORE_DB_URI" -v ON_ERROR_STOP=1 -c \
  'SELECT (SELECT count(*) FROM "user") AS users, (SELECT count(*) FROM pod) AS pods, (SELECT count(*) FROM deck) AS decks, (SELECT count(*) FROM game) AS games, (SELECT count(*) FROM game_participant) AS participants;'
