"""Public surface hardening (S333): the demo guest cannot reach per-user stores, the API map is
off by default, and a session id from the URL can only name a file in the caller's folder.
No network. Run: server/.venv/bin/python -m pytest tests"""

import importlib
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))

os.environ.setdefault("JWT_SECRET", "test-secret-not-real-0123456789")

from fastapi.testclient import TestClient  # noqa: E402


@pytest.fixture()
def client(tmp_path, monkeypatch):
    # The public cloud demo: every unauthenticated caller is the shared "guest"
    monkeypatch.setenv("DEMO_MODE", "true")
    monkeypatch.setenv("DEMO_MODE_PUBLIC", "true")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.delenv("API_DOCS", raising=False)
    import auth
    import main
    importlib.reload(auth)
    importlib.reload(main)
    return TestClient(main.app), auth


def test_guest_cannot_use_sessions_or_workflows(client):
    c, _ = client
    assert c.get("/api/v1/auth/me").json()["user_id"] == "guest"   # the guest still exists
    assert c.get("/api/v1/sessions").status_code == 403
    assert c.post("/api/v1/sessions", json={"resume": False}).status_code == 403
    assert c.get("/api/v1/sessions/abc").status_code == 403
    assert c.post("/api/v1/workflows", json={"workflow_type": "default", "goal": "x"}).status_code == 403
    assert c.get("/api/v1/workflows/abc").status_code == 403


def test_member_sessions_are_private(client):
    c, auth = client
    a = {"Authorization": "Bearer " + auth.make_token("user-a", "A")}
    b = {"Authorization": "Bearer " + auth.make_token("user-b", "B")}
    sid = c.post("/api/v1/sessions", json={"resume": False}, headers=a).json()["session_id"]
    assert [s["session_id"] for s in c.get("/api/v1/sessions", headers=a).json()] == [sid]
    assert c.get("/api/v1/sessions", headers=b).json() == []
    assert c.get(f"/api/v1/sessions/{sid}", headers=b).status_code == 404


def test_malformed_session_id_is_not_found(client):
    c, auth = client
    a = {"Authorization": "Bearer " + auth.make_token("user-a", "A")}
    for bad in ("a.b", "..", "x" * 65, "a%2E%2E"):
        assert c.get(f"/api/v1/sessions/{bad}", headers=a).status_code == 404


def test_api_map_off_by_default(client):
    c, _ = client
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert c.get(path).status_code == 404
