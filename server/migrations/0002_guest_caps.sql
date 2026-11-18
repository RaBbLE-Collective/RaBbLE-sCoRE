-- Per-guest daily turns (the cap that keeps one guest from draining the shared pool).
CREATE TABLE guest_turns_by_user (
    day     TEXT NOT NULL,
    user_id TEXT NOT NULL,
    n       INTEGER NOT NULL,
    PRIMARY KEY (day, user_id)
);

-- Guest accounts issued per UTC hour (the issuance rate limit survives restarts).
CREATE TABLE guest_issue (
    hour TEXT PRIMARY KEY,
    n    INTEGER NOT NULL
);
