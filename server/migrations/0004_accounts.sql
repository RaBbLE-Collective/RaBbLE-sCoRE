-- AC2 sign-in. Portable SQL (SQLite and Postgres): text keys generated in code, INTEGER 0/1 flags,
-- UTC ISO TEXT timestamps. A sign-in account is a users row (tier "free") plus one accounts row.
CREATE TABLE accounts (
    user_id              TEXT PRIMARY KEY,
    username             TEXT NOT NULL,
    username_lower       TEXT NOT NULL,
    email                TEXT NOT NULL,
    email_lower          TEXT NOT NULL,
    password_hash        TEXT NOT NULL,
    email_verified       INTEGER NOT NULL DEFAULT 0,
    sessions_valid_after TEXT NOT NULL DEFAULT '',
    created_at           TEXT NOT NULL,
    updated_at           TEXT NOT NULL
);
CREATE UNIQUE INDEX accounts_username_lower ON accounts (username_lower);
CREATE UNIQUE INDEX accounts_email_lower ON accounts (email_lower);

-- Single-use email tokens (kind: verify | reset). Only the sha256 of the token is stored.
CREATE TABLE auth_tokens (
    token_hash TEXT PRIMARY KEY,
    user_id    TEXT NOT NULL,
    kind       TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    consumed   INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX auth_tokens_user ON auth_tokens (user_id, kind);

-- Rotating refresh tokens, sha256 only. family_id ties a rotation chain together: presenting an
-- already-used token revokes the whole family (reuse detection).
CREATE TABLE refresh_tokens (
    token_hash TEXT PRIMARY KEY,
    user_id    TEXT NOT NULL,
    family_id  TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    used       INTEGER NOT NULL DEFAULT 0,
    revoked    INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX refresh_tokens_user ON refresh_tokens (user_id);
CREATE INDEX refresh_tokens_family ON refresh_tokens (family_id);
