import pytest


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
              "GUEST_GROQ_CUTOFF"):
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
