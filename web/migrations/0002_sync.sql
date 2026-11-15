-- Existing databases only (fresh ones get this from schema.sql). Run once:
--   npm run db:migrate-sync
-- uid identifies an attempt that came from the CLI, so pushing it twice is harmless.
ALTER TABLE attempts ADD COLUMN uid TEXT;
CREATE UNIQUE INDEX IF NOT EXISTS attempts_uid ON attempts(user_id, uid);
