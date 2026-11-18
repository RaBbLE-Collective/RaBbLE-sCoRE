-- Revoking a Pocket now deletes its row. Remove the ones revoked before that change.
DELETE FROM devices WHERE revoked = 1;
