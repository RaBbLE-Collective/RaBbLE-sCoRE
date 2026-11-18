-- Which firmware each Pocket runs: the Pocket reports its version in the beam hello; the newest report is kept per device
-- (and when it was heard), so the portal and the Pocket page can show it. Portable SQL (SQLite and Postgres).
ALTER TABLE devices ADD COLUMN fw TEXT NOT NULL DEFAULT '';
ALTER TABLE devices ADD COLUMN fw_seen TEXT NOT NULL DEFAULT '';
