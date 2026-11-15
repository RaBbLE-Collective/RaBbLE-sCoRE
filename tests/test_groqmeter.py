"""Guests stop when Groq's own rate-limit headers show a window past the cutoff. Fake headers only."""

import asyncio
import importlib
import time

import httpx
import pytest
from fastapi.testclient import TestClient

import beam
import db
import groqmeter
import guests
import llm


def _h(kind="requests", limit=1000, remaining=900, reset="30s"):
    return {f"x-ratelimit-limit-{kind}": str(limit), f"x-ratelimit-remaining-{kind}": str(remaining),
            f"x-ratelimit-reset-{kind}": reset}


def test_parse_reset():
    assert groqmeter.parse_reset("2m59.56s") == pytest.approx(179.56)
    assert groqmeter.parse_reset("7.66s") == pytest.approx(7.66)
    assert groqmeter.parse_reset("1h2m3s") == 3723
    assert groqmeter.parse_reset("250ms") == pytest.approx(0.25)
    assert groqmeter.parse_reset("") == 0 and groqmeter.parse_reset("soon") == 0


def test_no_data_means_no_cutoff(data_dir):
    assert groqmeter.guest_refusal() is None
    assert groqmeter.state()["windows"] == []


def test_under_at_and_over_the_cutoff(data_dir):
    groqmeter.record("chat", "m", _h(limit=1000, remaining=600))     # 40% used
    assert groqmeter.guest_refusal() is None
    groqmeter.record("chat", "m", _h(limit=1000, remaining=500))     # exactly 50%: still allowed
    assert groqmeter.guest_refusal() is None
    groqmeter.record("chat", "m", _h(limit=1000, remaining=499))     # past it
    why = groqmeter.guest_refusal()
    assert why and "chat requests" in why and "50%" in why
    groqmeter.record("chat", "m", _h(limit=1000, remaining=800))     # headroom is back
    assert groqmeter.guest_refusal() is None


def test_any_window_over_the_cutoff_refuses(data_dir):
    groqmeter.record("chat", "m", {**_h("requests", 1000, 900), **_h("tokens", 8000, 1000, "5s")})
    assert "tokens" in groqmeter.guest_refusal()
    groqmeter.record("tts", "orpheus", _h("requests", 100, 99))
    assert len(groqmeter.state()["windows"]) == 3


def test_a_window_that_has_reset_counts_as_empty(data_dir, monkeypatch):
    groqmeter.record("chat", "m", _h(limit=100, remaining=5, reset="1m"))
    assert groqmeter.guest_refusal()
    real = time.time
    monkeypatch.setattr(groqmeter.time, "time", lambda: real() + 61)
    assert groqmeter.guest_refusal() is None
    assert groqmeter.state()["windows"] == []


def test_cutoff_is_configurable(data_dir, monkeypatch):
    groqmeter.record("chat", "m", _h(limit=100, remaining=40))       # 60% used
    assert groqmeter.guest_refusal()
    monkeypatch.setenv("GUEST_GROQ_CUTOFF", "0.8")
    assert groqmeter.guest_refusal() is None


def test_headers_without_a_pair_are_ignored(data_dir):
    groqmeter.record("chat", "m", {"x-ratelimit-limit-requests": "10"})
    groqmeter.record("chat", "m", {"x-ratelimit-limit-requests": "abc", "x-ratelimit-remaining-requests": "1"})
    groqmeter.record("chat", "m", {"content-type": "x"})
    assert groqmeter.state()["windows"] == []


def test_a_short_copy_survives_a_restart(data_dir):
    groqmeter.record("chat", "m", _h(limit=100, remaining=1, reset="5m"))
    groqmeter._save(time.time(), force=True)
    groqmeter._limits.clear()
    groqmeter._loaded = False                                         # a new process
    assert groqmeter.guest_refusal()


def test_guest_spend_checks_groq_before_the_pools(data_dir):
    assert guests.spend("g1") is None
    groqmeter.record("chat", "m", _h(limit=100, remaining=10))
    assert "Groq chat requests" in guests.spend("g1")
    assert guests.turns_today() == 1                                  # the refusal spent nothing


def test_chat_headers_are_recorded_for_groq_only(data_dir):
    meta = {"provider": "groq", "tier": "fast", "model": "gpt-oss"}
    llm._log_response_status("complete_chat", meta, 200, httpx.Headers(_h(limit=100, remaining=20)))
    assert groqmeter.state()["windows"][0]["model"] == "gpt-oss"
    groqmeter.reset()
    db.execute("DELETE FROM meta WHERE key = 'groq_limits'", track=False)   # forget the persisted copy too
    llm._log_response_status("complete_chat", {**meta, "provider": "openrouter"}, 200, httpx.Headers(_h()))
    assert groqmeter.state()["windows"] == []


def test_stt_and_tts_calls_record_their_headers(data_dir):
    def handler(request):
        return httpx.Response(200 if "transcriptions" in str(request.url) else 429,
                              json={"text": "hi"}, headers=_h("requests", 100, 30))
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    import os
    os.environ["GROQ_API_KEY"] = "x"

    async def go():
        await beam.transcribe(client, b"\x00\x00" * 8000)
        with pytest.raises(beam.SpeechLimited):
            await beam.synthesize(client, "hello")
    asyncio.run(go())
    assert {w["endpoint"] for w in groqmeter.state()["windows"]} == {"stt", "tts"}


# ── through the app ───────────────────────────────────────────────────────────

@pytest.fixture()
def app(data_dir, monkeypatch):
    monkeypatch.setenv("DEMO_MODE", "true")
    monkeypatch.setenv("DEMO_MODE_PUBLIC", "true")
    monkeypatch.setenv("GUEST_ISSUE_SECRET", "s3")
    import auth
    import auth_routes
    import main
    importlib.reload(auth)
    importlib.reload(main)
    monkeypatch.setattr(auth_routes, "_ADMIN_KEY", "ak")

    async def fake_complete(messages, system, **kw):
        return "ok"
    monkeypatch.setattr(main.llm, "complete_chat", fake_complete)
    return TestClient(main.app)


def test_guests_are_refused_while_members_keep_going(app):
    key = app.post("/api/v1/users/guest", headers={"X-Guest-Secret": "s3"}).json()["api_key"]
    import users
    users.create_user(users.UserProfile(id="m", handle="mem", display_name="M", join_date="x", intention="",
                                        api_key="rbbl_member"))
    body = {"messages": [{"role": "user", "content": "hi"}], "model_tier": "fast"}
    post = lambda k: app.post("/api/v1/chat/complete", json=body, headers={"X-API-Key": k})
    assert post(key).status_code == 200                               # cold start: only the pools apply
    groqmeter.record("chat", "m", _h(limit=1000, remaining=100))
    r = post(key)
    assert r.status_code == 429 and "Groq chat requests" in r.json()["detail"]
    assert post("rbbl_member").status_code == 200                     # members keep the headroom
    groqmeter.record("chat", "m", _h(limit=1000, remaining=900))
    assert post(key).status_code == 200


def test_inventory_shows_the_tracker_and_cutoff_state(app):
    groqmeter.record("tts", "orpheus", _h("requests", 100, 20))
    j = app.get("/api/v1/admin/inventory", headers={"X-Admin-Key": "ak"}).json()
    assert j["groq"]["cutoff"] == 0.5 and "tts requests" in j["groq"]["refusing_guests"]
    w = j["groq"]["windows"][0]
    assert (w["endpoint"], w["limit"], w["remaining"], w["used"]) == ("tts", 100, 20, 0.8)
    assert j["r2"]["snapshots"]["state"] == "disabled"


def test_beam_guest_is_told_why(data_dir):
    class WS:
        sent = []

        async def send_text(self, t):
            import json
            self.sent.append(json.loads(t))
    ws = WS()
    ws.sent = []
    s = beam.BeamSession(ws, {"sub": "g1", "name": "G", "tier": "guest"})
    groqmeter.record("stt", "whisper", _h("requests", 100, 10))
    assert asyncio.run(s.guest_ok(4)) is False
    assert ws.sent[-1]["text"] == "GUEST LIMIT REACHED" and "stt requests" in ws.sent[-1]["reason"]
    assert guests.turns_today() == 0
