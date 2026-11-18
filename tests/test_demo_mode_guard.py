"""DEMO_MODE guest access is loopback-only unless DEMO_MODE_PUBLIC (server/auth.py).
Run: server/.venv/bin/python -m pytest tests"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))

os.environ.setdefault("JWT_SECRET", "test-secret-not-real-0123456789")

import auth  # noqa: E402
from fastapi import Depends, FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

app = FastAPI()


@app.get("/who")
async def who(user: dict = Depends(auth.require_user)):
    return user


def _client(host):
    return TestClient(app, client=(host, 50000))


@pytest.fixture
def demo(monkeypatch):
    monkeypatch.setattr(auth, "DEMO_MODE", True)
    monkeypatch.setattr(auth, "DEMO_MODE_PUBLIC", False)


def test_loopback_guest_allowed(demo):
    for host in ("127.0.0.1", "::1"):
        r = _client(host).get("/who")
        assert r.status_code == 200 and r.json()["sub"] == "guest"


def test_guest_is_pinned_off_cli_providers(demo):
    assert _client("127.0.0.1").get("/who").json()["llm_backend"] == "hosted_groq"


def test_non_loopback_guest_refused(demo):
    assert _client("192.168.1.31").get("/who").status_code == 401


def test_forwarded_loopback_refused(demo):
    r = _client("127.0.0.1").get("/who", headers={"X-Forwarded-For": "192.168.1.31"})
    assert r.status_code == 401


def test_non_loopback_guest_allowed_when_public(demo, monkeypatch):
    monkeypatch.setattr(auth, "DEMO_MODE_PUBLIC", True)
    assert _client("203.0.113.9").get("/who").json()["sub"] == "guest"


def test_demo_off_refuses_loopback_guest(monkeypatch):
    monkeypatch.setattr(auth, "DEMO_MODE", False)
    assert _client("127.0.0.1").get("/who").status_code == 401


def test_authenticated_unaffected(demo):
    tok = auth.make_token("u1", "Mark")
    r = _client("192.168.1.31").get("/who", headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 200 and r.json()["sub"] == "u1"


def test_bad_token_not_downgraded_to_guest(demo):
    r = _client("127.0.0.1").get("/who", headers={"Authorization": "Bearer nope"})
    assert r.status_code == 401


def test_startup_guard(monkeypatch):
    monkeypatch.setattr(auth, "DEMO_MODE", True)
    monkeypatch.setattr(auth, "DEMO_MODE_PUBLIC", False)
    monkeypatch.delenv("BIND_HOST", raising=False)
    monkeypatch.delenv("HOST", raising=False)
    with pytest.raises(RuntimeError):
        auth.startup_guard(["uvicorn", "main:app", "--host", "0.0.0.0"])
    with pytest.raises(RuntimeError):
        auth.startup_guard(["uvicorn", "--host=192.168.1.5"])
    auth.startup_guard(["uvicorn", "--host", "127.0.0.1"])
    auth.startup_guard(["uvicorn", "main:app"])
    monkeypatch.setattr(auth, "DEMO_MODE_PUBLIC", True)
    auth.startup_guard(["uvicorn", "--host", "0.0.0.0"])
    monkeypatch.setattr(auth, "DEMO_MODE_PUBLIC", False)
    monkeypatch.setattr(auth, "DEMO_MODE", False)
    auth.startup_guard(["uvicorn", "--host", "0.0.0.0"])
