CREATE TABLE IF NOT EXISTS cards(
  id TEXT PRIMARY KEY, topic TEXT NOT NULL, question TEXT NOT NULL, ref TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS card_state(
  user_id TEXT NOT NULL, card_id TEXT NOT NULL,
  ease REAL NOT NULL DEFAULT 2.5, interval REAL NOT NULL DEFAULT 0,
  reps INTEGER NOT NULL DEFAULT 0, due TEXT NOT NULL DEFAULT '',
  PRIMARY KEY(user_id, card_id));
CREATE TABLE IF NOT EXISTS attempts(
  id INTEGER PRIMARY KEY, user_id TEXT NOT NULL, card_id TEXT NOT NULL, ts TEXT NOT NULL,
  secs REAL, answer TEXT, correct INTEGER, depth INTEGER, clarity INTEGER, score REAL,
  detail TEXT, uid TEXT);
CREATE INDEX IF NOT EXISTS attempts_user_ts ON attempts(user_id, ts);
CREATE UNIQUE INDEX IF NOT EXISTS attempts_uid ON attempts(user_id, uid);
CREATE TABLE IF NOT EXISTS usage(
  user_id TEXT NOT NULL, day TEXT NOT NULL, calls INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY(user_id, day));
CREATE TABLE IF NOT EXISTS mock_sessions(
  id INTEGER PRIMARY KEY, user_id TEXT NOT NULL, ts TEXT NOT NULL, problem TEXT NOT NULL,
  turns TEXT NOT NULL, review TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS mock_sessions_user ON mock_sessions(user_id, id);
