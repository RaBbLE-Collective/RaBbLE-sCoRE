"""ResendMailer: request shape, retries, no leaks in logs, and signup readiness. No network."""

import logging

import pytest
from fastapi.testclient import TestClient

import mailer
import rate_limit

KEY = "fake-key-for-tests"
TEXT = "Verify: https://joinrabble.world/verify-email?token=TOPSECRETTOKEN"


@pytest.fixture()
def calls(monkeypatch):
    monkeypatch.setenv("MAIL_PROVIDER", "resend")
    monkeypatch.setenv("RESEND_API_KEY", KEY)
    monkeypatch.setenv("MAIL_FROM", "RaBbLE <noreply@joinrabble.world>")
    monkeypatch.setattr(mailer.time, "sleep", lambda s: None)
    box = {"calls": [], "replies": [(200, {"id": "abc"})]}

    def fake(url, headers, body, timeout):
        box["calls"].append((url, headers, body, timeout))
        r = box["replies"][min(len(box["calls"]) - 1, len(box["replies"]) - 1)]
        if isinstance(r, Exception):
            raise r
        return r

    monkeypatch.setattr(mailer, "_post", fake)
    return box


def test_request_shape(calls):
    m = mailer.get_mailer()
    assert isinstance(m, mailer.ResendMailer)
    m.send("a@example.com", "Hi", TEXT)
    (url, headers, body, timeout), = calls["calls"]
    assert url == "https://api.resend.com/emails"
    assert headers["Authorization"] == f"Bearer {KEY}"
    assert body == {"from": "RaBbLE <noreply@joinrabble.world>", "to": ["a@example.com"], "subject": "Hi", "text": TEXT}
    assert timeout == 10


@pytest.mark.parametrize("missing", ["RESEND_API_KEY", "MAIL_FROM"])
def test_missing_config_raises(calls, monkeypatch, missing):
    monkeypatch.delenv(missing)
    with pytest.raises(RuntimeError) as e:
        mailer.get_mailer()
    assert missing in str(e.value) and KEY not in str(e.value)


def test_4xx_raises_without_retry(calls, caplog):
    calls["replies"] = [(422, {"name": "validation_error", "message": "bad"})]
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(RuntimeError):
            mailer.get_mailer().send("a@example.com", "Hi", TEXT)
    assert len(calls["calls"]) == 1
    assert "422" in caplog.text and "validation_error" in caplog.text


def test_5xx_retries_once(calls):
    calls["replies"] = [(503, {}), (200, {"id": "x"})]
    mailer.get_mailer().send("a@example.com", "Hi", TEXT)
    assert len(calls["calls"]) == 2
    calls["calls"].clear()
    calls["replies"] = [(500, {}), (500, {})]
    with pytest.raises(RuntimeError):
        mailer.get_mailer().send("a@example.com", "Hi", TEXT)
    assert len(calls["calls"]) == 2


def test_network_error_retries_once(calls):
    calls["replies"] = [OSError("boom " + KEY), (200, {"id": "x"})]
    mailer.get_mailer().send("a@example.com", "Hi", TEXT)
    assert len(calls["calls"]) == 2


def test_no_secrets_in_logs(calls, caplog):
    calls["replies"] = [OSError("boom " + KEY), (500, {"message": "oops"}), ]
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(RuntimeError) as e:
            mailer.get_mailer().send("a@example.com", "Hi", TEXT)
    out = caplog.text + str(e.value)
    for bad in (KEY, "TOPSECRETTOKEN", "verify-email", "a@example.com"):
        assert bad not in out


def test_signup_ready_with_resend(calls, data_dir, monkeypatch):
    import accounts_routes
    monkeypatch.delenv("REQUIRE_VERIFIED_EMAIL", raising=False)
    monkeypatch.setenv("DEMO_MODE", "false")
    rate_limit._windows.clear()
    mailer.set_mailer(None)
    assert accounts_routes._mail_ready() is True
    import main
    c = TestClient(main.app)
    r = c.post("/api/v1/auth/signup", json={"username": "mark", "email": "mark@example.com", "password": "correct horse battery"})
    assert r.status_code == 201
    assert len(calls["calls"]) == 1
    assert calls["calls"][0][2]["to"] == ["mark@example.com"]
    rate_limit._windows.clear()
