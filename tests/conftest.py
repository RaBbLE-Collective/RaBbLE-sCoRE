import pytest


@pytest.fixture(autouse=True)
def _sqlite_unless_parametrized(monkeypatch):
    """Render (and a developer shell) export DATABASE_URL: tests run on SQLite unless they ask for
    the `backend` fixture, which opts into Postgres explicitly via TEST_DATABASE_URL."""
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("DATABASE_SCHEMA", raising=False)


@pytest.fixture(autouse=True)
def _isolated_data_dir(tmp_path_factory, monkeypatch):
    """Usage metering and account events write to the db on every turn: no test may touch ./data/score.db.
    Tests that want a clean db of their own ask for data_dir, which overrides this."""
    import db
    monkeypatch.setenv("DATA_DIR", str(tmp_path_factory.mktemp("data")))
    db.close()
    yield
    db.close()


@pytest.fixture(autouse=True)
def _no_real_usage_tracker(tmp_path_factory, monkeypatch):
    """The header reads the OS tracker's cache; tests must not depend on the machine they run on."""
    monkeypatch.setenv("RABBLE_USAGE_FILE", str(tmp_path_factory.mktemp("usage") / "none.json"))


# ── hosted persistence: db, R2 and guests (all offline) ──────────────────────

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))
os.environ.setdefault("JWT_SECRET", "test-secret-not-real-0123456789")


class FakeR2:
    """The five-method client storage.py expects, in a dict. calls records (op, key)."""

    def __init__(self):
        self.objects: dict[str, bytes] = {}
        self.calls: list[tuple[str, str]] = []
        self.fail = False

    def _do(self, op, key):
        self.calls.append((op, key))
        if self.fail:
            raise RuntimeError("R2 down")

    def put(self, key, data, content_type="application/octet-stream", if_match=None, if_none_match=None):
        self._do("put", key)
        import hashlib
        import storage
        cur = self.objects.get(key)
        if if_none_match == "*" and cur is not None:
            raise storage.PreconditionFailed("412 exists")
        if if_match and (cur is None or hashlib.md5(cur).hexdigest() != if_match):
            raise storage.PreconditionFailed("412 etag mismatch")
        self.objects[key] = data

    def get(self, key):
        self._do("get", key)
        return self.objects.get(key)

    def head(self, key):
        self._do("head", key)
        return {"size": len(self.objects[key])} if key in self.objects else None

    def list(self, prefix=""):
        self._do("list", prefix)
        return [{"key": k, "size": len(v), "last_modified": ""} for k, v in sorted(self.objects.items())
                if k.startswith(prefix)]

    def delete(self, key):
        self._do("delete", key)
        self.objects.pop(key, None)


@pytest.fixture()
def data_dir(tmp_path, monkeypatch):
    """A fresh DATA_DIR with a clean db, local-only storage and no guest env."""
    import db
    import storage
    for k in ("R2_ENDPOINT", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET", "R2_PREFIX",
              "R2_CLASS_A_BUDGET", "R2_CLASS_B_BUDGET", "GUEST_ISSUE_SECRET", "GUEST_DAILY_TURNS",
              "TRANSCRIPT_BACKEND", "R2_ALLOW_FRESH_START", "SNAPSHOT_KEEP", "SNAPSHOT_ALLOW_SHRINK",
              "GUEST_GROQ_CUTOFF", "GUEST_GROQ_MINUTE_CUTOFF"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    db.close()
    db.clear_dirty()
    storage.set_client(None)
    storage._warned.clear()
    import snapshot
    snapshot.state, snapshot.hold, snapshot._known, snapshot._marker_done = "disabled", None, None, False
    snapshot.baseline, snapshot.latest = {"users": 0, "invites": 0}, {}
    snapshot.fallback_hold, snapshot._ptr_etag = None, snapshot._UNKNOWN
    snapshot._protect.clear()
    snapshot._last_put_key = None
    monkeypatch.setenv("SNAPSHOT_POINTER_RETRY_S", "0")
    for k in ("SNAPSHOT_ACCEPT_FALLBACK", "SNAPSHOT_KEEP_HOURLY_H", "SNAPSHOT_KEEP_DAILY_D"):
        monkeypatch.delenv(k, raising=False)
    import groqmeter
    groqmeter.reset()
    yield tmp_path
    db.close()
    storage.set_client(None)


@pytest.fixture()
def r2(data_dir):
    """R2 mode with a fake bucket (budgets default)."""
    import storage
    fake = FakeR2()
    storage.set_client(fake)
    return fake


@pytest.fixture(autouse=True)
def _audit_to_tmp(tmp_path_factory, monkeypatch):
    """Beam and chat tests log to the audit file: keep it out of the repo."""
    import audit
    monkeypatch.setattr(audit, "LOG_PATH", str(tmp_path_factory.mktemp("audit") / "audit.jsonl"))


# ── Postgres backend for parametrized tests ──────────────────────────────────
# TEST_DATABASE_URL must be set explicitly (a DIRECT, non-pooled url: search_path is session state).
# Each test gets a throwaway schema, dropped afterwards. DATABASE_URL is never used here.

@pytest.fixture(params=["sqlite", "postgres"])
def backend(request, monkeypatch):
    import uuid
    import db
    if request.param == "sqlite":
        yield "sqlite"
        return
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL not set")
    if "-pooler" in url:
        pytest.skip("TEST_DATABASE_URL must be a direct (non-pooled) url")
    schema = "test_" + uuid.uuid4().hex[:12]
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("DATABASE_SCHEMA", schema)
    yield "postgres"
    db.close()
    import psycopg
    with psycopg.connect(url, autocommit=True) as c:
        c.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
