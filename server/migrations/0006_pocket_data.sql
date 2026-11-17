-- Pocket captures and conversations kept per member account, readable by the web portal.
-- Portable SQL (SQLite and Postgres): ids are text made in code (uuid hex), timestamps are UTC ISO TEXT
-- (db.now_ts, fixed width, so string order is time order), as in 0005_devices.sql.
CREATE TABLE pocket_conversations (
    id         TEXT PRIMARY KEY,
    user_id    TEXT NOT NULL,
    device     TEXT NOT NULL,
    started_at TEXT NOT NULL,
    ended_at   TEXT
);
CREATE INDEX pocket_conversations_user ON pocket_conversations (user_id, started_at DESC);
CREATE TABLE pocket_turns (
    id              TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES pocket_conversations (id) ON DELETE CASCADE,
    user_id         TEXT NOT NULL,
    role            TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    text            TEXT NOT NULL,
    ts              TEXT NOT NULL
);
CREATE INDEX pocket_turns_conversation ON pocket_turns (conversation_id, ts);
CREATE INDEX pocket_turns_user ON pocket_turns (user_id, ts DESC);
CREATE TABLE pocket_notes (
    user_id TEXT NOT NULL,
    id      TEXT NOT NULL,
    device  TEXT NOT NULL,
    title   TEXT NOT NULL,
    source  TEXT NOT NULL CHECK (source IN ('voice', 'text')),
    created TEXT NOT NULL,
    md      TEXT NOT NULL,
    ts      TEXT NOT NULL,
    PRIMARY KEY (user_id, id)
);
CREATE INDEX pocket_notes_user ON pocket_notes (user_id, ts DESC);
