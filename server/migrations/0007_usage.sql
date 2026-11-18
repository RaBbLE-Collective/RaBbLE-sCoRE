-- Usage metering, per-account limits and a durable account event log (S347).
-- Portable SQL (SQLite and Postgres): text ids made in code, UTC ISO TEXT timestamps (db.now_ts, fixed width,
-- so string order is time order), as in 0005_devices.sql and 0006_pocket_data.sql.

-- One row per metered call: a model turn, a speech-to-text or text-to-speech request. Every user and interaction is
-- recorded, whatever the tier (admin included); limits are checked against these rows.
--   kind: turn (a user-facing model exchange, counted against *_turns), tool (a tool-calling pre-pass),
--         stt and tts (speech, counted against *_audio_s)
--   status: HTTP-style (200 ok, 429 provider or limit, 5xx failed); only status < 400 counts toward a limit
CREATE TABLE usage_events (
    id         TEXT PRIMARY KEY,
    ts         TEXT NOT NULL,
    user_id    TEXT NOT NULL,
    kind       TEXT NOT NULL,
    workflow   TEXT,
    provider   TEXT,
    model      TEXT,
    tokens_in  INTEGER,
    tokens_out INTEGER,
    audio_s    DOUBLE PRECISION,
    chars      INTEGER,
    status     INTEGER,
    device     TEXT,
    ms         INTEGER
);
CREATE INDEX usage_events_user_ts ON usage_events (user_id, ts);
CREATE INDEX usage_events_ts ON usage_events (ts);

-- Per-account overrides of the tier defaults. NULL = use the tier default; -1 = unlimited.
CREATE TABLE account_limits (
    user_id          TEXT PRIMARY KEY,
    daily_turns      INTEGER,
    monthly_turns    INTEGER,
    daily_audio_s    INTEGER,
    monthly_audio_s  INTEGER,
    updated_by       TEXT,
    updated_at       TEXT,
    note             TEXT
);

-- Account events, durable (audit.jsonl on Render is ephemeral): signup, login, logout, verify, reset, device,
-- invite, limit changes and limit hits. ip_hash is a one-way hash, never an address; detail never holds a password.
CREATE TABLE account_events (
    id       TEXT PRIMARY KEY,
    ts       TEXT NOT NULL,
    user_id  TEXT,
    event    TEXT NOT NULL,
    ip_hash  TEXT,
    detail   TEXT
);
CREATE INDEX account_events_user_ts ON account_events (user_id, ts);
CREATE INDEX account_events_ts ON account_events (ts);
