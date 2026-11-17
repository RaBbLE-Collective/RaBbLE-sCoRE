"""users.py on the db: same public API as the JSON files, plus the one-time import."""

import json

import pytest
from datetime import datetime, timedelta, timezone

import db
import users
from users import InviteToken, UserProfile


# Every test here runs on SQLite, and on Postgres when TEST_DATABASE_URL is set (see conftest.backend).

@pytest.fixture(autouse=True)
def _backend(backend):
    return backend


sqlite_only = pytest.mark.usefixtures("_sqlite_only")


@pytest.fixture()
def _sqlite_only(backend):
    if backend != "sqlite":
        pytest.skip("SQLite-specific")


def _profile(i="u1", handle="h1", key="rbbl_a"):
    return UserProfile(id=i, handle=handle, display_name="N", join_date="2026-01-01", intention="x",
                       api_key=key, byo_key_enc="enc")


def test_user_round_trip_and_lookups(data_dir):
    users.create_user(_profile())
    assert users.get_user("u1").handle == "h1"
    assert users.get_user("nope") is None
    assert users.get_user_by_handle("h1").id == "u1"
    assert users.get_user_by_api_key("rbbl_a").id == "u1"
    assert users.get_user_by_api_key("rbbl_zzz") is None
    p = users.get_user("u1")
    p.intention = "changed"
    users.save_user(p)
    assert users.get_user("u1").intention == "changed"
    assert users.list_handles() == {"h1"}
    assert users.get_user("u1").byo_key_enc == "enc"


def test_invites(data_dir):
    inv = users.create_invite("admin", "note", 7)
    assert users.get_invite(inv.token).note == "note"
    assert users.consume_invite(inv.token, "u1") is True
    assert users.consume_invite(inv.token, "u2") is False       # once only
    assert users.get_invite(inv.token).consumed_by == "u1"
    assert [i.token for i in users.list_invites()] == [inv.token]
    assert users.consume_invite("missing", "u1") is False
    old = users.create_invite("admin", "", 7)
    expired = old.model_copy(update={"expires_at": (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()})
    users._save_invite(expired)
    assert users.consume_invite(old.token, "u1") is False


def test_json_files_import_once(data_dir):
    for sub in ("users", "invites"):
        (data_dir / sub).mkdir(exist_ok=True)
    (data_dir / "users" / "u9.json").write_text(_profile("u9", "old", "rbbl_old").model_dump_json())
    (data_dir / "users" / "bad.json").write_text("{not json")
    inv = InviteToken(token="tok", created_by="a", created_at="2026-01-01T00:00:00+00:00",
                      expires_at="2099-01-01T00:00:00+00:00", consumed=True, consumed_by="u9")
    (data_dir / "invites" / "tok.json").write_text(inv.model_dump_json())
    assert users.get_user("u9").handle == "old"
    assert users.get_invite("tok").consumed is True
    # edit a user in the db; a reopen must not re-import the stale file over it
    p = users.get_user("u9")
    p.handle = "new"
    users.save_user(p)
    db.close()
    assert users.get_user("u9").handle == "new"
    assert (data_dir / "users" / "u9.json").exists()           # files left in place


def test_migrations_are_recorded_and_idempotent(data_dir):
    db.conn()
    db.close()
    db.conn()
    rows = db.query("SELECT version FROM schema_migrations")
    assert [r["version"] for r in rows] == ["0001_init", "0002_guest_caps", "0003_updated_at", "0004_accounts", "0005_devices", "0006_pocket_data", "0007_usage"]


@sqlite_only
def test_backup_is_a_consistent_copy(data_dir, tmp_path):
    users.create_user(_profile())
    out = tmp_path / "copy.db"
    db.backup(out)
    import sqlite3
    assert sqlite3.connect(out).execute("SELECT handle FROM users").fetchone()[0] == "h1"


def test_writes_mark_dirty_but_bookkeeping_does_not(data_dir):
    db.conn()
    db.clear_dirty()
    db.meta_set("k", "v")
    assert db.dirty_since is None
    users.create_user(_profile())
    assert (db.dirty_since is not None) == (not db.is_postgres())   # snapshots are SQLite-only


@sqlite_only
def test_failed_migration_leaves_nothing_half_applied(tmp_path, monkeypatch):
    import sqlite3
    mig = tmp_path / "mig"
    mig.mkdir()
    (mig / "0001_a.sql").write_text("CREATE TABLE a (x INTEGER);\n")
    (mig / "0002_b.sql").write_text("-- comment; with semicolon\nCREATE TABLE b (x INTEGER);\nCREATE TABLE a (x INTEGER);\n")
    monkeypatch.setattr(db, "MIGRATIONS", mig)
    c = sqlite3.connect(tmp_path / "t.db")
    with pytest.raises(sqlite3.OperationalError):
        db._migrate(c)
    tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "a" in tables and "b" not in tables                       # 0002 rolled back whole
    assert [r[0] for r in c.execute("SELECT version FROM schema_migrations")] == ["0001_a"]
    (mig / "0002_b.sql").write_text("CREATE TABLE b (x INTEGER);\n")   # fixed: the retry just applies it
    db._migrate(c)
    assert [r[0] for r in c.execute("SELECT version FROM schema_migrations")] == ["0001_a", "0002_b"]


def test_api_keys_are_hashed_at_rest(data_dir):
    users.create_user(_profile(key="rbbl_plain"))
    raw = db.query_one("SELECT api_key FROM users WHERE id = 'u1'")["api_key"]
    assert raw.startswith("sha256:") and "rbbl_plain" not in raw
    assert users.get_user_by_api_key("rbbl_plain").id == "u1"        # lookup hashes the input
    assert users.get_user_by_api_key(raw) is None                    # a leaked hash is not a credential
    p = users.get_user("u1")
    users.save_user(p)                                               # a read-modify-save does not double hash
    assert users.get_user_by_api_key("rbbl_plain").id == "u1"


def test_legacy_plaintext_rows_are_hashed_on_open(data_dir):
    db.conn()
    db.execute("INSERT INTO users (id, handle, display_name, join_date, intention, llm_backend, tier, api_key) "
               "VALUES ('o', 'old', 'O', 'x', '', 'hosted_openrouter', 'free', 'rbbl_legacy')")
    db.close()
    assert users.get_user_by_api_key("rbbl_legacy").id == "o"
    assert db.query_one("SELECT api_key FROM users WHERE id = 'o'")["api_key"].startswith("sha256:")
