-- Conversations and turns carry the surface they came from, so a chat started on one surface can be picked up on another.
-- origin: the surface that opened the conversation; surface (per turn): where that message was said.
-- 'pocket' | 'web' | 'tui' | 'desktop'. The list shows 'blended' when turns came from more than one surface (derived, not stored).
-- Rows from before this migration were all made by a Pocket, which is the default. Portable SQL (SQLite and Postgres).
ALTER TABLE pocket_conversations ADD COLUMN origin TEXT NOT NULL DEFAULT 'pocket';
ALTER TABLE pocket_turns ADD COLUMN surface TEXT NOT NULL DEFAULT 'pocket';
