"""accounts table: export and restore-merge keep sign-in accounts (SQLite)."""

import accounts
import db


def test_export_includes_accounts(data_dir):
    accounts.create_account("mark", "m@example.com", "correct horse battery")
    out = db.export_json()
    assert [r["username"] for r in out["accounts"]] == ["mark"]


def test_restore_merge_keeps_local_account(data_dir):
    snap = data_dir / "snap.db"
    db.conn()
    db.backup(snap)                                   # empty snapshot, schema only
    accounts.create_account("mark", "m@example.com", "correct horse battery")
    db.replace_with(snap.read_bytes(), merge_local=True)
    assert accounts.by_username("mark") is not None
    assert accounts.by_username("mark")["email_lower"] == "m@example.com"


def test_restore_merge_collision_leaves_no_orphan_user(data_dir):
    """M3: a local-only account whose email the snapshot already uses is dropped with its users row."""
    db.conn()
    accounts.create_account("snapuser", "e@x.com", "correct horse battery")
    snap = data_dir / "snap.db"
    db.backup(snap)
    data = snap.read_bytes()
    db.execute("DELETE FROM accounts")
    db.execute("DELETE FROM users")
    accounts.create_account("localuser", "e@x.com", "correct horse battery")
    db.replace_with(data, merge_local=True)
    db.close()
    db.conn()
    uids = {r["id"] for r in db.query("SELECT id FROM users")}
    aids = {r["user_id"] for r in db.query("SELECT user_id FROM accounts")}
    assert uids == aids and len(aids) == 1
    assert accounts.by_username("snapuser") and not accounts.by_username("localuser")


def test_cap_count_and_purge_on_both_backends(backend, data_dir, monkeypatch):
    """The cap count and the unverified purge are plain SQL: same answer on SQLite and Postgres."""
    monkeypatch.setenv("REQUIRE_VERIFIED_EMAIL", "true")
    a = accounts.create_account("verified1", "v1@example.com", "correct horse battery")
    accounts.create_account("ghost1", "g1@example.com", "correct horse battery")
    accounts.mark_verified(a["user_id"])
    assert accounts.count_accounts() == 1
    monkeypatch.setenv("REQUIRE_VERIFIED_EMAIL", "false")
    assert accounts.count_accounts() == 2
    monkeypatch.setenv("REQUIRE_VERIFIED_EMAIL", "true")
    db.execute("UPDATE accounts SET created_at = '2000-01-01T00:00:00.000000Z' WHERE email_verified = 0")
    accounts.issue_token(a["user_id"], "verify")
    accounts.purge_expired()
    assert [r["username"] for r in db.query("SELECT username FROM accounts")] == ["verified1"]
    assert db.query_one("SELECT COUNT(*) AS n FROM users")["n"] == 1


def test_restore_merge_keeps_local_revocations(data_dir):
    """A refresh family revoked and a reset token spent locally stay so after a late restore of an older snapshot."""
    db.conn()
    a = accounts.create_account("mark", "m@example.com", "correct horse battery")
    reset = accounts.issue_token(a["user_id"], "reset")
    refresh = accounts.issue_refresh(a["user_id"])
    snap = data_dir / "snap.db"
    db.backup(snap)                                      # tokens live, family unrevoked
    accounts.consume_token(reset, "reset")
    accounts.revoke_presented(refresh)
    db.replace_with(snap.read_bytes(), merge_local=True)
    db.close()
    db.conn()
    assert accounts.peek_token(reset, "reset") is None
    assert accounts.rotate_refresh(refresh) is None


def test_export_and_restore_merge_keep_devices(data_dir):
    """Local-only devices are added by a late restore. (A device deleted locally after the snapshot is NOT
    remembered by a SQLite snapshot merge: production runs on Postgres where no merge happens.)"""
    import devices
    db.conn()
    a = accounts.create_account("mark", "m@example.com", "correct horse battery")
    kept = devices.create(a["user_id"], "old")
    snap = data_dir / "snap.db"
    db.backup(snap)                                      # old device live in the snapshot
    new = devices.create(a["user_id"], "new")            # local-only since
    assert {r["id"] for r in db.export_json()["devices"]} == {kept["id"], new["id"]}
    db.replace_with(snap.read_bytes(), merge_local=True)
    db.close()
    db.conn()
    assert devices.resolve(kept["key"])["id"] == a["user_id"]
    assert devices.resolve(new["key"])["id"] == a["user_id"]
