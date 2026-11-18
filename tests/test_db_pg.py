"""Backend-parametrized db behaviour beyond users: R2 usage accounting, guest counters,
the placeholder translator, first-boot concurrency and write-retry safety (Postgres needs TEST_DATABASE_URL)."""

import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

import db
import guests
import storage

SERVER = str(Path(__file__).resolve().parent.parent / "server")


@pytest.fixture(autouse=True)
def _backend(backend, data_dir):
    return backend


def test_r2_usage_record_usage_and_merge(r2):
    storage.record("class_a")
    storage.record("class_a")
    storage.record("class_b")
    u = storage.usage()
    assert (u["class_a"], u["class_b"]) == (2, 1)
    storage.merge_usage(storage._month(), 1, 9)          # keeps the larger of each
    u = storage.usage()
    assert (u["class_a"], u["class_b"]) == (2, 9)
    storage.merge_usage(storage._month(), 5, 3)
    u = storage.usage()
    assert (u["class_a"], u["class_b"]) == (5, 9)
    storage.put("k", b"x")                                # a real counted call through the fake client
    assert storage.usage()["class_a"] == 6


def test_guest_counters(monkeypatch):
    monkeypatch.setenv("GUEST_DAILY_TURNS", "2")
    monkeypatch.setenv("GUEST_ISSUE_PER_HOUR", "2")
    assert [guests.take_turn("u1") for _ in range(3)] == [True, True, False]
    assert guests.turns_today() == 2
    assert guests.take_issue_slot() is True


def test_export_json_has_every_table():
    db.meta_set("a", "b")
    out = db.export_json()
    assert set(out) == set(db.EXPORT_TABLES)
    assert {"key": "a", "value": "b"} in out["meta"]


def test_reads_leave_no_open_transaction(backend):
    if backend != "postgres":
        pytest.skip("Postgres only")
    db.query("SELECT 1 AS x")
    from psycopg.pq import TransactionStatus
    assert db.conn().info.transaction_status == TransactionStatus.IDLE


def test_translate_skips_literals_comments_and_quoted_names():
    t = db._translate
    assert t("SELECT 1", 0) == "SELECT 1"
    assert t("a = ? AND b LIKE 'x?%' AND c = ?", 2) == "a = %s AND b LIKE 'x?%%' AND c = %s"
    assert t('SELECT "we?ird" FROM t WHERE a = ? -- why?\n AND b = ?', 2) == 'SELECT "we?ird" FROM t WHERE a = %s -- why?\n AND b = %s'
    assert t("/* ? */ a = ?", 1) == "/* ? */ a = %s"
    assert t(r"a = E'it\'s ?' AND b = ?", 1) == r"a = E'it\'s ?' AND b = %s"
    assert t("a = 'it''s ?' AND b = ?", 1) == "a = 'it''s ?' AND b = %s"
    with pytest.raises(ValueError):
        t("SELECT $$ ? $$, ?", 1)


def test_unparseable_url_does_not_leak(backend, monkeypatch):
    if backend != "postgres":
        pytest.skip("Postgres only")
    monkeypatch.setenv("DATABASE_URL", '"postgresql://user:hunter2secret@nohost.invalid/db"')
    db.close()
    with pytest.raises(RuntimeError) as e:
        db.conn()
    assert "hunter2secret" not in str(e.value)
    # nothing may be reachable by walking the exception chain, either
    assert e.value.__cause__ is None and e.value.__context__ is None


def test_schema_refused_on_pooled_url(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@ep-x-pooler.example.invalid/db")
    monkeypatch.setenv("DATABASE_SCHEMA", "s")
    db.close()
    with pytest.raises(ValueError, match="direct"):
        db.conn()


class _DropsAfterApply:
    """Runs the statement for real, then reports the connection dying before the ack."""

    def __init__(self, real, times=1):
        self._real, self.left = real, times

    def __getattr__(self, name):
        return getattr(self._real, name)

    def execute(self, q, p=None):
        cur = self._real.execute(q, p)
        if self.left and not q.lstrip().upper().startswith("SELECT 1"):
            self.left -= 1
            import psycopg
            raise psycopg.OperationalError("server closed the connection unexpectedly")
        return cur


def test_a_failed_write_is_not_retried(backend):
    if backend != "postgres":
        pytest.skip("Postgres only")
    import psycopg
    day = "2026-01-01"
    db.execute("INSERT INTO guest_turns (day, n) VALUES (?, 0)", (day,))
    real = db.conn()
    db._conn = _DropsAfterApply(real)
    with pytest.raises(psycopg.OperationalError):
        db.execute("UPDATE guest_turns SET n = n + 1 WHERE day = ?", (day,))
    db.close()
    assert db.query_one("SELECT n FROM guest_turns WHERE day = ?", (day,))["n"] == 1   # applied once, not twice


def test_a_read_is_retried_on_a_dropped_connection(backend):
    if backend != "postgres":
        pytest.skip("Postgres only")
    db.meta_set("k", "v")

    class DropsBeforeRead(_DropsAfterApply):
        def execute(self, q, p=None):
            if self.left:
                self.left -= 1
                import psycopg
                raise psycopg.OperationalError("server closed the connection unexpectedly")
            return self._real.execute(q, p)

    db._conn = DropsBeforeRead(db.conn())
    assert db.meta_get("k") == "v"


def test_concurrent_first_boot(backend):
    if backend != "postgres":
        pytest.skip("Postgres only")
    env = dict(os.environ, PYTHONPATH=SERVER, DATABASE_SCHEMA="test_" + uuid.uuid4().hex[:12])
    env.pop("DATA_DIR", None)
    code = "import db; db.conn(); print(len(db.query('SELECT version FROM schema_migrations')))"
    try:
        procs = [subprocess.Popen([sys.executable, "-c", code], env=env, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, text=True) for _ in range(3)]
        outs = [p.communicate(timeout=120) + (p.returncode,) for p in procs]
        assert all(rc == 0 for _, _, rc in outs), [e[-300:] for _, e, _ in outs]
        assert {o.strip() for o, _, _ in outs} == {str(len(os.listdir(os.path.join(SERVER, "migrations"))))}
    finally:
        import psycopg
        with psycopg.connect(env["DATABASE_URL"], autocommit=True) as c:
            c.execute(f'DROP SCHEMA IF EXISTS "{env["DATABASE_SCHEMA"]}" CASCADE')
