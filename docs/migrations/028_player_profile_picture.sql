-- Existing PostgreSQL deployments: apply before starting the updated app.
-- SQLite uses the idempotent 028_player_profile_picture startup migration.
-- Fresh installations include this nullable column in the declarative schema.
BEGIN;
ALTER TABLE player ADD COLUMN IF NOT EXISTS profile_picture_url VARCHAR(500);
COMMIT;
