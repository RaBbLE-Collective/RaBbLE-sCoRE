"""Per-device guests, the shared daily budget, admin inventory/export, /chat transcripts. No network."""

import asyncio
import importlib
import io
import json
import tarfile

import pytest
from fastapi.testclient import TestClient

import beam
import db
import guests
import storage
import users


@pytest.fixture()
def app(data_dir, monkeypatch):
    monkeypatch.setenv("DEMO_MODE", "true")
    monkeypatch.setenv("DEMO_MODE_PUBLIC", "true")
    monkeypatch.setenv("GUEST_ISSUE_SECRET", "s3cret-issue")
    monkeypatch.delenv("API_DOCS", raising=False)
    import audit
    import auth
    import auth_routes
    import main
    importlib.reload(auth)
    importlib.reload(main)
    monkeypatch.setattr(auth_routes, "_ADMIN_KEY", "admin-key-test")
    monkeypatch.setattr(audit, "LOG_PATH", str(data_dir / "audit.jsonl"))
    return TestClient(main.app), main


def _guest(c, label=None, secret="s3cret-issue"):
    body = {"label": label} if label else None
    return c.post("/api/v1/users/guest", json=body, headers={"X-Guest-Secret": secret})


def test_issue_is_off_without_the_env(app, monkeypatch):
    c, _ = app
    monkeypatch.delenv("GUEST_ISSUE_SECRET")
    assert _guest(c).status_code == 404


def test_issue_needs_the_secret(app):
    c, _ = app
    assert _guest(c, secret="nope").status_code == 403
    assert c.post("/api/v1/users/guest").status_code == 403


def test_issue_creates_a_real_guest_account(app):
    c, _ = app
    r = _guest(c, "Mark's pocket")
    assert r.status_code == 201
    body = r.json()
    assert set(body) == {"api_key", "handle"}
    assert body["handle"].startswith("guest-") and len(body["handle"]) == 12
    p = users.get_user_by_api_key(body["api_key"])
    assert (p.tier, p.llm_backend, p.display_name) == ("guest", "hosted_groq", "Mark's pocket")
    assert _guest(c).json()["handle"] != body["handle"]
    assert c.post("/api/v1/users/guest", headers={"X-Guest-Secret": "s3cret-issue"}).status_code == 201  # no body


def test_guest_is_pinned_to_groq_and_kept_out_of_members_only_routes(app):
    c, _ = app
    key = _guest(c).json()["api_key"]
    h = {"X-API-Key": key}
    assert c.get("/api/v1/auth/me", headers=h).json()["tier"] == "guest"
    assert c.get("/api/v1/sessions", headers=h).status_code == 403
    assert c.post("/api/v1/workflows", json={"workflow_type": "default", "goal": "x"}, headers=h).status_code == 403
    assert c.put("/api/v1/users/me", json={"llm_backend": "hosted_openrouter"}, headers=h).status_code == 403
    import auth
    import asyncio as a
    claims = a.run(auth.require_user(None, None, key))
    assert claims["llm_backend"] == "hosted_groq"
    # the JWT route keeps the tier, so members-only still holds
    tok = c.post("/api/v1/auth/token", json={"api_key": key}).json()["token"]
    assert c.get("/api/v1/sessions", headers={"Authorization": f"Bearer {tok}"}).status_code == 403


def test_issue_refused_with_503_when_r2_budget_is_spent(app, r2, monkeypatch):
    c, _ = app
    monkeypatch.setenv("R2_CLASS_A_BUDGET", "1")
    storage.put("x", b"1")
    r = _guest(c)
    assert r.status_code == 503 and "next month" in r.json()["detail"]
    monkeypatch.setattr(storage, "_month", lambda: "2099-01")     # next month: resumes
    assert _guest(c).status_code == 201


def test_shared_daily_budget_on_chat(app, monkeypatch):
    c, main = app
    monkeypatch.setenv("GUEST_DAILY_TURNS", "3")

    async def fake_complete(messages, system, **kw):
        return "ok"
    monkeypatch.setattr(main.llm, "complete_chat", fake_complete)
    keys = [_guest(c).json()["api_key"] for _ in range(2)]
    body = {"messages": [{"role": "user", "content": "hi"}], "model_tier": "fast"}
    codes = [c.post("/api/v1/chat/complete", json=body, headers={"X-API-Key": keys[i % 2]}).status_code
             for i in range(4)]
    assert codes == [200, 200, 200, 429]                           # one pool across both guests
    r = c.post("/api/v1/chat/complete", json=body, headers={"X-API-Key": keys[0]})
    assert r.status_code == 429 and "Guest limit" in r.json()["detail"]
    assert guests.turns_today() == 3
    # members are not counted against it
    member = users.UserProfile(id="m1", handle="mem", display_name="M", join_date="x", intention="",
                               api_key="rbbl_member")
    users.create_user(member)
    assert c.post("/api/v1/chat/complete", json=body, headers={"X-API-Key": "rbbl_member"}).status_code == 200


def test_one_guest_cannot_drain_the_shared_pool(app, monkeypatch):
    monkeypatch.setenv("GUEST_DAILY_TURNS", "10")
    monkeypatch.setenv("GUEST_PER_GUEST_DAILY_TURNS", "3")
    assert [guests.take_turn("a") for _ in range(5)] == [True, True, True, False, False]
    assert guests.turns_today() == 3                      # refusals spend nothing from the pool
    assert [guests.take_turn(x) for x in "bcd"] == [True, True, True]
    monkeypatch.setenv("GUEST_DAILY_TURNS", "7")
    assert guests.take_turn("e") is True and guests.take_turn("e") is False
    assert guests.turns_today() == 7                      # pool gone: e's refused turn was handed back
    assert db.query_one("SELECT n FROM guest_turns_by_user WHERE user_id = 'e'")["n"] == 1


def test_per_guest_cap_applies_on_chat(app, monkeypatch):
    c, main = app
    monkeypatch.setenv("GUEST_PER_GUEST_DAILY_TURNS", "2")

    async def fake_complete(messages, system, **kw):
        return "ok"
    monkeypatch.setattr(main.llm, "complete_chat", fake_complete)
    k1, k2 = (_guest(c).json()["api_key"] for _ in range(2))
    body = {"messages": [{"role": "user", "content": "hi"}], "model_tier": "fast"}
    post = lambda k: c.post("/api/v1/chat/complete", json=body, headers={"X-API-Key": k}).status_code
    assert [post(k1), post(k1), post(k1), post(k2)] == [200, 200, 429, 200]


def test_a_throttled_chat_spends_no_guest_turn(app, monkeypatch):
    c, main = app
    import rate_limit
    monkeypatch.setattr(rate_limit, "USER_LIMITS", {**rate_limit.USER_LIMITS, "guest": {"per_minute": 1, "per_hour": 100, "per_day": 100}})

    async def fake_complete(messages, system, **kw):
        return "ok"
    monkeypatch.setattr(main.llm, "complete_chat", fake_complete)
    key = _guest(c).json()["api_key"]
    body = {"messages": [{"role": "user", "content": "hi"}], "model_tier": "fast"}
    codes = [c.post("/api/v1/chat/complete", json=body, headers={"X-API-Key": key}).status_code for _ in range(4)]
    assert codes == [200, 429, 429, 429]
    assert guests.turns_today() == 1


def test_issuance_is_rate_limited_per_hour_and_survives_restart(app, monkeypatch):
    c, _ = app
    monkeypatch.setenv("GUEST_ISSUE_PER_HOUR", "2")
    assert [_guest(c).status_code for _ in range(3)] == [201, 201, 429]
    assert "Too many" in _guest(c).json()["detail"]
    db.close()                                            # a restart: the count is in the db
    assert _guest(c).status_code == 429


def test_admin_key_alone_is_enough_and_nothing_else_is(app, data_dir):
    c, _ = app
    key = {"X-Admin-Key": "admin-key-test"}
    for path in ("/api/v1/admin/inventory", "/api/v1/admin/export"):
        assert c.get(path, headers=key).status_code == 200
        assert c.get(path, headers={"X-Admin-Key": "wrong"}).status_code == 403   # demo guest, wrong key
        assert c.get(path, headers={"X-API-Key": _guest(c).json()["api_key"]}).status_code == 403
    assert c.post("/api/v1/admin/invites", json={}, headers=key).status_code == 201


def test_admin_key_works_with_demo_mode_off(app, monkeypatch):
    c, _ = app
    import auth
    monkeypatch.setattr(auth, "DEMO_MODE", False)         # no demo guest to fall back on
    key = {"X-Admin-Key": "admin-key-test"}
    assert c.get("/api/v1/admin/inventory", headers=key).status_code == 200
    assert c.get("/api/v1/admin/inventory").status_code == 401
    assert c.get("/api/v1/admin/inventory", headers={"X-Admin-Key": "nope"}).status_code == 401
    # a Bearer admin-tier token still works
    tok = auth.make_token("someone", "S", tier="admin")
    assert c.get("/api/v1/admin/inventory", headers={"Authorization": f"Bearer {tok}"}).status_code == 200
    member = auth.make_token("m", "M", tier="collective")
    assert c.get("/api/v1/admin/inventory", headers={"Authorization": f"Bearer {member}"}).status_code == 403


def test_export_includes_only_the_data_dir_audit_log(app, data_dir, monkeypatch, tmp_path_factory):
    import audit
    c, _ = app
    (data_dir / "audit.jsonl").write_text("inside\n")
    outside = tmp_path_factory.mktemp("elsewhere") / "audit.jsonl"
    outside.write_text("outside\n")
    key = {"X-Admin-Key": "admin-key-test"}

    def audit_in_tar():
        r = c.get("/api/v1/admin/export", headers=key)
        t = tarfile.open(fileobj=io.BytesIO(r.content), mode="r:gz")
        return t.extractfile("audit.jsonl").read() if "audit.jsonl" in t.getnames() else None
    assert audit_in_tar() == b"inside\n"
    monkeypatch.setattr(audit, "LOG_PATH", str(outside))
    assert audit_in_tar() is None                         # a log outside DATA_DIR is never exported


def test_audit_defaults_under_data_dir(monkeypatch, tmp_path):
    import audit
    monkeypatch.delenv("AUDIT_LOG_PATH", raising=False)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    importlib.reload(audit)
    assert audit.LOG_PATH == str(tmp_path / "audit.jsonl")
    audit.log("u", "x", 200)
    assert (tmp_path / "audit.jsonl").exists()


def test_inventory_shows_restore_state(app, r2):
    c, _ = app
    import snapshot
    snapshot.state = "failed"
    j = c.get("/api/v1/admin/inventory", headers={"X-Admin-Key": "admin-key-test"}).json()
    assert j["r2"]["restore"] == "failed" and j["r2"]["uploads_enabled"] is False


# ── beam isolation ────────────────────────────────────────────────────────────

class _WS:
    def __init__(self, key):
        self.headers = {"authorization": f"Bearer {key}"}
        self.sent = []

    async def send_text(self, t):
        self.sent.append(json.loads(t))


def test_two_guests_cannot_see_each_others_notes_or_history(app, monkeypatch):
    c, _ = app
    beam._kept.clear()
    beam._history.clear()
    a_key, b_key = (_guest(c).json()["api_key"] for _ in range(2))
    ua, ub = beam._authorize(_WS(a_key)), beam._authorize(_WS(b_key))
    assert ua["sub"] != ub["sub"] and ua["tier"] == ub["tier"] == "guest"
    assert ua["llm_backend"] == "hosted_groq"
    wa, wb = _WS(a_key), _WS(b_key)
    sa, sb = beam.BeamSession(wa, ua), beam.BeamSession(wb, ub)
    sa.device = sb.device = "same-device-name"                    # even a colliding device name
    beam._keep_note(sa.history_key(), {"id": "n1", "title": "A's", "md": "# a secret", "path": "p", "ts": __import__("time").time()})
    beam._history[sa.history_key()] = [{"role": "user", "content": "a private thing"}]
    asyncio.run(sb.resend_kept())
    assert wb.sent == []                                          # B is sent nothing
    asyncio.run(sa.resend_kept())
    assert [m["id"] for m in wa.sent] == ["n1"]
    assert sb.history_key() not in beam._history
    # B acking A's note id does not drop it
    asyncio.run(sb.on_text(json.dumps({"type": "note.saved", "id": "n1"})))
    assert len(beam._kept[sa.history_key()]) == 1


def test_guest_beam_chain_is_groq_only_and_has_no_node(app, monkeypatch):
    c, _ = app
    monkeypatch.setenv("BEAM_LLM_CHAIN", "claude_code:claude-sonnet-4-6,groq:openai/gpt-oss-20b")
    monkeypatch.setattr(beam.node, "node_route", lambda sub: [{"provider": "node", "model": "x"}])
    key = _guest(c).json()["api_key"]
    s = beam.BeamSession(_WS(key), beam._authorize(_WS(key)))
    assert {n["provider"] for n in s.turn_chain()} == {"groq"}
    member = beam.BeamSession(_WS("k"), {"sub": "m", "name": "M", "tier": "free"})
    assert member.turn_chain()[0]["provider"] == "node"


def test_beam_guest_turn_stops_at_the_shared_limit(app, monkeypatch):
    c, _ = app
    monkeypatch.setenv("GUEST_DAILY_TURNS", "1")
    key = _guest(c).json()["api_key"]
    ws = _WS(key)
    s = beam.BeamSession(ws, beam._authorize(ws))
    assert asyncio.run(s.guest_ok(1)) is True
    assert asyncio.run(s.guest_ok(2)) is False
    assert ws.sent[-1]["text"] == "GUEST LIMIT REACHED" and ws.sent[-1]["turn"] == 2 and ws.sent[-1]["reason"]


# ── admin ─────────────────────────────────────────────────────────────────────

def test_inventory_is_counts_only_and_admin_only(app, data_dir):
    c, _ = app
    _guest(c)
    _guest(c)
    users.create_invite("admin", "secret note")
    (data_dir / "transcripts").mkdir()
    (data_dir / "transcripts" / "2026-11-15.jsonl").write_text('{"messages":"PRIVATE"}\n{"x":1}\n')
    (data_dir / "audit.jsonl").write_text("a\nb\nc\n")
    assert c.get("/api/v1/admin/inventory").status_code == 403   # the demo guest is not admin
    assert c.get("/api/v1/admin/inventory", headers={"X-API-Key": _guest(c).json()["api_key"]}).status_code == 403
    r = c.get("/api/v1/admin/inventory", headers={"X-Admin-Key": "admin-key-test"})
    assert r.status_code == 200
    j = r.json()
    assert j["users"]["guests"] == 3 and j["users"]["by_tier"] == {"guest": 3}
    assert j["invites"] == {"total": 1, "consumed": 0}
    assert j["audit_lines"] >= 3 and j["db"]["bytes"] > 0
    assert j["transcripts"]["entries"] == 2
    assert j["transcripts"]["files"] == [{"name": "2026-11-15.jsonl", "bytes": 31}]
    assert j["r2"]["mode"] == "local-only" and j["r2"]["budgets"]["class_a"] == 800000
    assert "PRIVATE" not in r.text and "secret note" not in r.text and "rbbl_" not in r.text


def test_inventory_reports_r2_counters(app, r2):
    c, _ = app
    storage.put("x", b"1")
    j = c.get("/api/v1/admin/inventory", headers={"X-Admin-Key": "admin-key-test"}).json()
    assert j["r2"]["mode"] == "R2" and j["r2"]["usage"]["class_a"] == 1 and j["r2"]["exhausted"] is None


def test_export_is_a_tarball_with_db_audit_and_transcripts(app, data_dir):
    c, _ = app
    _guest(c)
    (data_dir / "transcripts").mkdir()
    (data_dir / "transcripts" / "d.jsonl").write_text("{}\n")
    (data_dir / "audit.jsonl").write_text("a\n")
    assert c.get("/api/v1/admin/export").status_code == 403
    r = c.get("/api/v1/admin/export", headers={"X-Admin-Key": "admin-key-test"})
    assert r.status_code == 200 and r.headers["content-type"] == "application/gzip"
    names = tarfile.open(fileobj=io.BytesIO(r.content), mode="r:gz").getnames()
    assert {"score.db", "audit.jsonl", "transcripts/d.jsonl"} <= set(names)
