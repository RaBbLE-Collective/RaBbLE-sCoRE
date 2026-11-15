-- Portable SQL: the same files run on SQLite now and Postgres later.
-- Booleans are INTEGER 0/1, timestamps are ISO-8601 TEXT.

CREATE TABLE users (
    id           TEXT PRIMARY KEY,
    handle       TEXT NOT NULL,
    display_name TEXT NOT NULL,
    email        TEXT,
    join_date    TEXT NOT NULL,
    intention    TEXT NOT NULL,
    llm_backend  TEXT NOT NULL,
    byo_key_enc  TEXT,
    tier         TEXT NOT NULL,
    api_key      TEXT NOT NULL
);
CREATE INDEX users_handle ON users (handle);
CREATE INDEX users_api_key ON users (api_key);

CREATE TABLE invites (
    token       TEXT PRIMARY KEY,
    created_by  TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    expires_at  TEXT NOT NULL,
    note        TEXT,
    consumed    INTEGER NOT NULL DEFAULT 0,
    consumed_by TEXT
);

-- One row per UTC day: the shared guest turn budget.
CREATE TABLE guest_turns (
    day TEXT PRIMARY KEY,
    n   INTEGER NOT NULL
);

-- R2 requests per UTC month, Class A (PUT/COPY/LIST/POST) and Class B (GET/HEAD).
CREATE TABLE r2_usage (
    month   TEXT PRIMARY KEY,
    class_a INTEGER NOT NULL DEFAULT 0,
    class_b INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
