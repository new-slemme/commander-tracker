# Production operations

## Launch shape

Run one application instance in an EU region behind an HTTPS reverse proxy. Keep infrastructure below roughly €50/month until recurring support exceeds cost. SQLite remains for local development and isolated tests; public registration uses managed PostgreSQL.

Required settings:

- `APP_ENV=production`, `FLASK_SECRET_KEY`, `COMMANDER_DB_URI=postgresql+psycopg://...`
- `TRUST_PROXY=1` behind one trusted reverse proxy; secure/HTTP-only/Lax cookies are automatic in production
- `SMTP_HOST`, `SMTP_PORT`, `SMTP_USERNAME`, `SMTP_PASSWORD`, `EMAIL_FROM`
- `OBJECT_STORAGE_BUCKET`, `OBJECT_STORAGE_ENDPOINT` (when non-AWS), `OBJECT_STORAGE_REGION`, and standard AWS credentials
- `SENTRY_DSN` optionally enables error reporting without default PII

`GET /healthz` is the uptime probe and checks database reachability. Logs are JSON in production. Alert on health failures, elevated 5xx responses, mail failures, and backup failures.

## Database changes

The numbered bootstrap migration remains a compatibility path for SQLite. A new PostgreSQL database is created from SQLAlchemy metadata. Once PostgreSQL contains production data, every schema change must ship as a reviewed Alembic migration; do not rely on `create_all()` to alter existing tables.

## Backups and restore drill

Run `scripts/backup_postgres.sh` daily with a separately stored `BACKUP_ENCRYPTION_KEY`. Copy encrypted artifacts to a second provider/region with lifecycle retention. The script defaults to 30 days.

Quarterly restore drill:

1. Create an empty, isolated PostgreSQL database with no application traffic.
2. Set `RESTORE_DB_URI` and the backup encryption key.
3. Run `scripts/restore_postgres.sh <backup.dump.enc>`.
4. Compare its user, pod, deck, game, and participant counts with production at the backup timestamp.
5. Start a disposable app instance against the restored DB and open one historic game recap.
6. Record date, backup identifier, counts, duration, and operator; then destroy the drill database.

Never run a restore against the live database. Restrict backup keys separately from database credentials.

## Object storage and card art

New user uploads use S3-compatible object storage and short-lived signed reads. Existing `/art/` files remain readable for migration compatibility. Official card art caching is still a launch review item: confirm Scryfall/Wizards policy and attribution before copying the legacy cache into production.

## Launch gates still outside code

Complete the operator-specific privacy/terms text, processor inventory, retention schedule, IP review, production penetration/security review, email-domain setup, and monitoring/backup vendor configuration before opening registration. Do not enable paid feature gates before the planned IP review.
