"""Pass 4: newer-wins merge with monotone consumed state, self-fence recovery, lineage check for a db
already on disk, 503 on credential writes while uploads are off, fence only on conditional puts, hold persistence."""

import hashlib
import importlib
import json
import sqlite3

import httpx
import pytest
from fastapi.testclient import TestClient

import db
import snapshot
import storage
import users
from users import UserProfile

P = "score/"
POINTER = P + "snapshots/latest.json"


def U(i, name="N"):
    return UserProfile(id=i, handle="h" + i, display_name=name, join_date="2026-01-01", intention="", api_key="k" + i)


@pytest.fixture()
def fresh(r2, monkeypatch):
    monkeypatch.setenv("R2_ALLOW_FRESH_START", "1")
    return r2


def _other_writer(r2):
    """B copies the current object to a new generation and moves the pointer (a deploy overlap)."""
    cur = snapshot.latest["key"]
    g = snapshot._gen(cur) + 1
    k = f"snapshots/score-g{g:08d}-20261115T000000Z.db"
    r2.objects[P + k] = r2.objects[P + cur]
    r2.objects[POINTER] = json.dumps({"key": k, "gen": g, "writer": "B", "users": snapshot.latest["users"],
                                      "invites": snapshot.latest["invites"],
                                      "sha256": hashlib.sha256(r2.objects[P + k]).hexdigest()}).encode()


def _forget():
    db.close()
    db.clear_dirty()
    snapshot.state, snapshot.hold, snapshot._known, snapshot._marker_done = "disabled", None, None, False
    snapshot.baseline, snapshot.latest = {"users": 0, "invites": 0}, {}
    snapshot.fallback_hold, snapshot._ptr_etag = None, snapshot._UNKNOWN
    snapshot._protect.clear()


# ── 1. newer wins, consumed only moves forward ───────────────────────────────

def test_edit_and_consume_made_while_fenced_survive_the_merge(fresh):
    snapshot.try_restore()
    users.create_user(U("a"))
    inv = users.create_invite("a")
    assert snapshot.flush()
    _other_writer(fresh)
    p = users.get_user("a"); p.display_name = "EDITED"; users.save_user(p)
    assert users.consume_invite(inv.token, "a")
    assert snapshot.flush() is False and snapshot.state == "failed"
    assert snapshot.try_restore() == "restored"
    assert users.get_user("a").display_name == "EDITED"
    assert users.get_invite(inv.token).consumed is True
    assert users.get_invite(inv.token).consumed_by == "a"


def test_an_older_local_row_does_not_beat_a_newer_restored_one(fresh):
    snapshot.try_restore()
    users.create_user(U("a", "old"))
    assert snapshot.flush()
    snap = db.db_path().read_bytes()
    p = users.get_user("a"); p.display_name = "newer"; users.save_user(p)
    assert snapshot.flush()
    # a stale local db (older row) merges into the newer snapshot
    db.close(); db.db_path().write_bytes(snap)
    _forget()
    assert snapshot.try_restore() == "restored"
    assert users.get_user("a").display_name == "newer"


def test_merge_never_unconsumes_an_invite(data_dir):
    inv = users.create_invite("a")
    restored = data_dir / "restored.db"
    db.backup(restored)
    sqlite = sqlite3.connect(restored)
    sqlite.execute("UPDATE invites SET consumed = 1, consumed_by = 'x', updated_at = '2026-01-01T00:00:00.000000Z'")
    sqlite.commit(); sqlite.close()
    # local row is NEWER and unconsumed: still must end consumed
    db.replace_with(restored.read_bytes(), merge_local=True)
    got = users.get_invite(inv.token)
    assert got.consumed is True and got.consumed_by == "x"


def test_migration_backfills_updated_at(data_dir):
    c = sqlite3.connect(db.db_path())
    for f in ("0001_init.sql", "0002_guest_caps.sql"):
        c.executescript((db.MIGRATIONS / f).read_text())
    c.execute("CREATE TABLE schema_migrations (version TEXT PRIMARY KEY, applied_at TEXT NOT NULL)")
    c.executemany("INSERT INTO schema_migrations VALUES (?, 'x')", [("0001_init",), ("0002_guest_caps",)])
    c.execute("INSERT INTO users (id,handle,display_name,join_date,intention,llm_backend,tier,api_key) "
              "VALUES ('u','h','n','2026-02-03T00:00:00+00:00','','x','free','k')")
    c.execute("INSERT INTO invites (token,created_by,created_at,expires_at) VALUES ('t','a','2026-02-04T00:00:00+00:00','z')")
    c.commit(); c.close()
    assert db.query_one("SELECT updated_at FROM users")["updated_at"] == "2026-02-03T00:00:00+00:00"
    assert db.query_one("SELECT updated_at FROM invites")["updated_at"] == "2026-02-04T00:00:00+00:00"


def test_every_write_path_sets_updated_at(data_dir):
    users.create_user(U("a"))
    t1 = db.query_one("SELECT updated_at FROM users")["updated_at"]
    p = users.get_user("a"); p.display_name = "x"; users.save_user(p)
    assert db.query_one("SELECT updated_at FROM users")["updated_at"] > t1
    inv = users.create_invite("a")
    t2 = db.query_one("SELECT updated_at FROM invites")["updated_at"]
    assert users.consume_invite(inv.token, "a")
    assert db.query_one("SELECT updated_at FROM invites")["updated_at"] > t2


# ── 2. a landed pointer write with a lost response does not fence us ─────────

def test_lost_pointer_response_is_adopted_not_fenced(fresh):
    snapshot.try_restore(); users.create_user(U("a")); assert snapshot.flush()
    real = fresh.put
    state = {"once": True}

    def put(key, data, ct="application/octet-stream", if_match=None, if_none_match=None):
        real(key, data, ct, if_match, if_none_match)
        if key == POINTER and state["once"]:
            state["once"] = False
            raise RuntimeError("read timeout")
    fresh.put = put
    users.create_user(U("b")); assert snapshot.flush() is False
    p = users.get_user("a"); p.display_name = "EDITED"; users.save_user(p)
    assert snapshot.flush() is True and snapshot.state != "failed"
    ptr = json.loads(fresh.objects[POINTER])
    assert ptr["writer"] == snapshot.INSTANCE and ptr["key"].endswith(".db")
    assert users.get_user("a").display_name == "EDITED"


def test_a_pointer_moved_by_someone_else_still_fences(fresh):
    snapshot.try_restore(); users.create_user(U("a")); assert snapshot.flush()
    _other_writer(fresh)
    users.create_user(U("b"))
    assert snapshot.flush() is False and snapshot.state == "failed"


# ── 3. a db on disk must descend from the pointer before it uploads ──────────

def test_stale_db_on_disk_is_merged_not_uploaded(fresh):
    snapshot.try_restore()
    for i in range(10):
        users.create_user(U(f"u{i}"))
    assert snapshot.flush()
    stale = db.db_path().read_bytes()
    users.create_user(U("late")); assert snapshot.flush()
    db.close(); db.db_path().write_bytes(stale)
    _forget()
    assert snapshot.try_restore() == "restored"          # merged, not "present"
    assert users.get_user("late") is not None
    users.create_user(U("laptop"))
    assert snapshot.flush()
    head = fresh.objects[P + json.loads(fresh.objects[POINTER])["key"]]
    c = sqlite3.connect(":memory:"); c.deserialize(head)
    assert {r[0] for r in c.execute("SELECT id FROM users")} >= {"late", "laptop", "u0"}


def test_restart_on_the_same_disk_stays_present(fresh):
    snapshot.try_restore(); users.create_user(U("a")); assert snapshot.flush()
    _forget()
    assert snapshot.try_restore() == "present"


def test_disk_with_no_lineage_is_merged(fresh):
    snapshot.try_restore(); users.create_user(U("a")); assert snapshot.flush()
    db.execute("DELETE FROM meta WHERE key = ?", (snapshot.LINEAGE,), track=False)
    _forget()
    assert snapshot.try_restore() == "restored"


# ── 4. credential writes answer 503 while uploads are off ────────────────────

@pytest.fixture()
def app(r2, monkeypatch):
    monkeypatch.setenv("DEMO_MODE", "true")
    monkeypatch.setenv("DEMO_MODE_PUBLIC", "true")
    monkeypatch.setenv("GUEST_ISSUE_SECRET", "s3cret-issue")
    monkeypatch.delenv("API_DOCS", raising=False)
    import audit, auth, auth_routes, main
    importlib.reload(auth)
    importlib.reload(main)
    monkeypatch.setattr(auth_routes, "_ADMIN_KEY", "admin-key-test")
    monkeypatch.setattr(audit, "LOG_PATH", str(db.db_path().parent / "audit.jsonl"))
    return TestClient(main.app)


@pytest.mark.parametrize("setup", ["failed", "hold", "fallback_hold"])
def test_credential_writes_are_503_while_uploads_are_off(app, setup):
    if setup == "failed":
        snapshot.state = "failed"
    elif setup == "hold":
        snapshot.hold = "shrink"
    else:
        snapshot.fallback_hold = {"source": "x", "reason": "r", "protect": []}
    g = app.post("/api/v1/users/guest", headers={"X-Guest-Secret": "s3cret-issue"})
    i = app.post("/api/v1/admin/invites", json={}, headers={"X-Admin-Key": "admin-key-test"})
    s = app.post("/api/v1/users/summon", json={"invite_token": "t", "handle": "abc", "display_name": "A", "intention": "",
                                               "llm_backend": "hosted_openrouter"})
    assert g.status_code == 503 and s.status_code == 503
    assert i.status_code == 503
    assert users.list_handles() == set()


def test_guest_issue_works_when_uploads_are_on(app):
    snapshot.state = "restored"
    assert app.post("/api/v1/users/guest", headers={"X-Guest-Secret": "s3cret-issue"}).status_code == 201


# ── 5. fence only on conditional puts; hold persistence fails closed ─────────

def _client(status):
    return storage.S3Client("https://a.r2.cloudflarestorage.com", "k", "s", "bkt",
                            http=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(status, text="x"))))


@pytest.mark.parametrize("status", [409, 412])
def test_unconditional_put_conflict_is_not_a_fence(status):
    c = _client(status)
    with pytest.raises(RuntimeError) as e:
        c.put("usage.json", b"{}")
    assert not isinstance(e.value, storage.PreconditionFailed)
    with pytest.raises(storage.PreconditionFailed):
        c.put("latest.json", b"{}", if_match="abc")
    with pytest.raises(storage.PreconditionFailed):
        c.put("score-g1.db", b"{}", if_none_match="*")


def test_usage_put_failure_does_not_fence_or_fail_the_flush(fresh):
    snapshot.try_restore(); users.create_user(U("a"))
    real = fresh.put

    def put(key, *a, **k):
        if key == storage.prefix() + "usage.json" or key.endswith(storage.USAGE_KEY):
            raise storage.PreconditionFailed("409")
        return real(key, *a, **k)
    fresh.put = put
    assert snapshot.flush() is True and snapshot.state != "failed"


def test_hold_not_persisted_fails_closed_and_loud(fresh, monkeypatch, caplog):
    snapshot.try_restore()
    users.create_user(U("a")); assert snapshot.flush()
    users.create_user(U("b")); assert snapshot.flush()
    fresh.objects[P + json.loads(fresh.objects[POINTER])["key"]] = b"garbage"
    _forget(); db.db_path().unlink()
    from pathlib import Path
    real_write, real_meta = Path.write_text, db.meta_set

    def boom(self, *a, **k):
        if self.name == ".restore-hold":
            raise OSError("disk full")
        return real_write(self, *a, **k)

    def meta_boom(key, *a, **k):
        if key == "fallback_hold":
            raise OSError("disk full")
        return real_meta(key, *a, **k)
    monkeypatch.setattr(Path, "write_text", boom)
    monkeypatch.setattr(db, "meta_set", meta_boom)
    with caplog.at_level("ERROR", logger="rabble.snapshot"):
        assert snapshot.try_restore() == "failed"
    assert "NOT PERSISTED" in caplog.text and not snapshot.uploads_enabled()


def test_hold_survives_a_restart_via_the_db_when_the_file_is_lost(fresh):
    snapshot.try_restore()
    users.create_user(U("a")); assert snapshot.flush()
    users.create_user(U("b")); assert snapshot.flush()
    fresh.objects[P + json.loads(fresh.objects[POINTER])["key"]] = b"garbage"
    _forget(); db.db_path().unlink()
    assert snapshot.try_restore() == "restored" and snapshot.fallback_hold
    snapshot._hold_file().unlink()                        # the file is lost, the db still carries it
    _forget()
    assert snapshot.try_restore() == "present" and snapshot.fallback_hold and not snapshot.uploads_enabled()
