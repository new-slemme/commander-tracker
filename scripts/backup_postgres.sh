#!/usr/bin/env bash
set -euo pipefail

: "${COMMANDER_DB_URI:?Set COMMANDER_DB_URI to the PostgreSQL connection URI}"
: "${BACKUP_ENCRYPTION_KEY:?Set BACKUP_ENCRYPTION_KEY for AES-256 encryption}"

backup_dir="${BACKUP_DIR:-/data/backups}"
retention_days="${BACKUP_RETENTION_DAYS:-30}"
mkdir -p "$backup_dir"
stamp="$(date -u +%Y%m%d-%H%M%SZ)"
output="$backup_dir/commander-$stamp.dump.enc"

pg_dump "$COMMANDER_DB_URI" --format=custom --no-owner --no-acl \
  | openssl enc -aes-256-cbc -pbkdf2 -salt -pass env:BACKUP_ENCRYPTION_KEY -out "$output"

test -s "$output"
find "$backup_dir" -type f -name 'commander-*.dump.enc' -mtime "+$retention_days" -delete
echo "encrypted PostgreSQL backup written: $output"
