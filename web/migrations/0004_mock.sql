-- Existing databases only: the mock interview sessions. Run once:
--   npm run db:migrate-mock
CREATE TABLE IF NOT EXISTS mock_sessions(
  id INTEGER PRIMARY KEY, user_id TEXT NOT NULL, ts TEXT NOT NULL, problem TEXT NOT NULL,
  turns TEXT NOT NULL, review TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS mock_sessions_user ON mock_sessions(user_id, id);
