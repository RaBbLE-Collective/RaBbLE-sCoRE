-- updated_at: when a row was last written (UTC, fixed-width ISO, so TEXT order is time order).
-- A restore merge takes the newer row when a row exists on both sides (db.replace_with).
-- Existing rows are backfilled with their creation time.
ALTER TABLE users ADD COLUMN updated_at TEXT NOT NULL DEFAULT '';
UPDATE users SET updated_at = join_date;
ALTER TABLE invites ADD COLUMN updated_at TEXT NOT NULL DEFAULT '';
UPDATE invites SET updated_at = created_at;
