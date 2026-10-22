-- Existing PostgreSQL deployments: apply before starting the updated app.
-- SQLite uses the idempotent 029_deck_declared_bracket startup migration.
BEGIN;
ALTER TABLE deck ADD COLUMN IF NOT EXISTS declared_bracket INTEGER
    CHECK (declared_bracket BETWEEN 1 AND 5);
COMMIT;
