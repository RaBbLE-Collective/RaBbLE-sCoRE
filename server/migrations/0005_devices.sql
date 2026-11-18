-- AC7 device keys: a paired Pocket holds a key minted by a signed-in member. Only the sha256 of the key is stored.
-- Portable SQL (SQLite and Postgres): text keys made in code, INTEGER 0/1 flags, UTC ISO TEXT timestamps.
CREATE TABLE devices (
    id         TEXT PRIMARY KEY,
    user_id    TEXT NOT NULL,
    label      TEXT NOT NULL,
    key_hash   TEXT NOT NULL,
    created_at TEXT NOT NULL,
    last_seen  TEXT NOT NULL DEFAULT '',
    revoked    INTEGER NOT NULL DEFAULT 0
);
CREATE UNIQUE INDEX devices_key_hash ON devices (key_hash);
CREATE INDEX devices_user ON devices (user_id);
