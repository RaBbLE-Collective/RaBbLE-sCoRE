"""Usage metering, per-account limits and the durable account event log (usage.py, account_events.py,
usage_routes.py, wiring in beam.py and main.py). No network. Store-level tests also run on Postgres
(TEST_DATABASE_URL); the HTTP and websocket tests run on SQLite."""

import json

import pytest
from fastapi.testclient import TestClient

import account_events
import audit
import beam
import db
import devices
import guests
import mailer
import rate_limit
import usage
import users

PW = "correct horse battery"


# ── fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture()
def env(data_dir, monkeypatch):
    for k in ("ACCOUNT_CAP", "REQUIRE_VERIFIED_EMAIL", "AUTH_IP_HEADER", "MAIL_PROVIDER", "SIGNUP_INVITE_ONLY",
              "RABBLE_ADMIN_KEY", "BEAM_DEVICE_TOKEN", "BEAM_ALLOW_GUEST", "BEAM_ALLOW_GUEST_POCKETS", "USAGE_DEFAULTS",
              "DEVICE_LIMIT"):
        monkeypatch.delenv(k, raising=False)
    for tier in ("FREE", "PRO", "ENTITY", "ADMIN", "COLLECTIVE", "GUEST"):
        for f in ("DAILY_TURNS", "MONTHLY_TURNS", "DAILY_AUDIO_S", "MONTHLY_AUDIO_S"):
            monkeypatch.delenv(f"USAGE_{tier}_{f}", raising=False)
    monkeypatch.setenv("DEMO_MODE", "false")
    import auth
    monkeypatch.setattr(auth, "DEMO_MODE", False)         # other tests reload auth with DEMO_MODE on
    rate_limit._windows.clear()
    usage._last_hit.clear()
    devices._touched.clear()
    mailer.set_mailer(mailer.ConsoleMailer())
    beam._kept.clear()
    beam._history.clear()
    calls = {"llm": 0, "complete": 0}

    async def fake_stream(messages, system, **kw):
        calls["llm"] += 1
        if kw.get("sink") is not None:
            kw["sink"].update(provider="groq", model="gpt-test", tokens_in=11, tokens_out=7)
        yield json.dumps({"title": "T", "tags": [], "summary": [], "notes": "n"}) if "tidy note" in system else "Hello "
        if "tidy note" not in system:
            yield "there."

    async def fake_complete(messages, system, **kw):
        calls["complete"] += 1
        if kw.get("sink") is not None:
            kw["sink"].update(provider="groq", model="gpt-test", tokens_in=20, tokens_out=5)
        return "ok"

    async def fake_stt(client, pcm, timeout=30):
        return "hello pocket"

    async def fake_tts(client, text, voice="daniel"):
        return b"\x00\x01" * 8000        # 0.5 s at 16 kHz

    monkeypatch.setattr(beam.llm, "stream_chat", fake_stream)
    monkeypatch.setattr(beam, "transcribe", fake_stt)
    monkeypatch.setattr(beam, "synthesize", fake_tts)
    import main
    monkeypatch.setattr(main.llm, "complete_chat", fake_complete)
    c = TestClient(main.app)
    c.calls = calls
    yield c
    mailer.set_mailer(None)
    rate_limit._windows.clear()
    usage._last_hit.clear()
    beam._kept.clear()
    beam._history.clear()


def signup(c, name="alice", tier=None):
    tok = users.create_invite(created_by="t").token
    r = c.post("/api/v1/auth/signup", json={"username": name, "email": f"{name}@example.com",
                                            "password": PW, "invite_token": tok})
    assert r.status_code == 201
    if tier:
        import accounts
        p = users.get_user(accounts.by_username(name)["user_id"])
        p.tier = tier
        users.save_user(p)
    return c.post("/api/v1/auth/login", json={"identifier": name, "password": PW})


def member(c, name="alice", tier=None):
    r = signup(c, name, tier).json()
    return {"Authorization": f"Bearer {r['token']}"}, r["user_id"]


def chat(c, h, **kw):
    return c.post("/api/v1/chat/complete", json={"messages": [{"role": "user", "content": "hi"}], "model_tier": "fast"},
                  headers=h, **kw)


def rows(sql, *p):
    return db.query(sql, p)


def _drain(ws, until="done"):
    msgs = []
    while True:
        m = json.loads(ws.receive_text())
        msgs.append(m)
        if m["type"] in (until, "error"):
            return msgs


def say(c, headers, text="hi pocket", device="P1", turn=1):
    with c.websocket_connect("/api/v1/beam", headers=headers) as ws:
        ws.send_text(json.dumps({"type": "hello", "device": device}))
        assert json.loads(ws.receive_text())["type"] == "ready"
        ws.send_text(json.dumps({"type": "turn.begin", "turn": turn, "mode": "text", "text": text}))
        return _drain(ws)


def voice(c, headers, secs=1.0, speak=False, device="P1", turn=1):
    with c.websocket_connect("/api/v1/beam", headers=headers) as ws:
        ws.send_text(json.dumps({"type": "hello", "device": device}))
        ws.receive_text()
        ws.send_text(json.dumps({"type": "turn.begin", "turn": turn, "mode": "voice", "speak": speak}))
        ws.send_bytes(bytes([beam.KIND_MIC, beam.CODEC_PCM16]) + (turn & 0xFFFF).to_bytes(2, "little")
                      + b"\x01\x00" * int(16000 * secs))
        ws.send_text(json.dumps({"type": "turn.end", "turn": turn}))
        msgs = []
        while True:
            m = ws.receive()
            if m.get("text") is None:
                continue
            msg = json.loads(m["text"])
            msgs.append(msg)
            if msg["type"] in ("done", "error"):
                return msgs


# ── recording ────────────────────────────────────────────────────────────────

def test_member_chat_is_recorded_with_tokens_and_model(env):
    h, uid = member(env)
    assert chat(env, h).status_code == 200
    (r,) = rows("SELECT * FROM usage_events WHERE user_id = ?", uid)
    assert (r["kind"], r["status"], r["provider"], r["model"], r["tokens_in"], r["tokens_out"]) == \
           ("turn", 200, "groq", "gpt-test", 20, 5)
    assert r["workflow"] and r["ms"] is not None


def test_beam_text_turn_records_turn_with_device(env):
    h, uid = member(env)
    say(env, h, device="RaBbLE-Pocket-5EB8")
    (r,) = rows("SELECT * FROM usage_events WHERE user_id = ?", uid)
    assert (r["kind"], r["workflow"], r["tokens_in"], r["tokens_out"], r["device"]) == \
           ("turn", "voice", 11, 7, "RaBbLE-Pocket-5EB8")


def test_beam_voice_turn_records_stt_seconds_and_tts_chars(env):
    h, uid = member(env)
    voice(env, h, secs=1.5)
    stt = rows("SELECT * FROM usage_events WHERE user_id = ? AND kind = 'stt'", uid)
    assert len(stt) == 1 and abs(stt[0]["audio_s"] - 1.5) < 0.01 and stt[0]["status"] == 200
    assert stt[0]["model"] == beam.STT_MODEL
    voice(env, h, secs=1.0, speak=True, turn=2)                   # now with speech out
    tts = rows("SELECT * FROM usage_events WHERE user_id = ? AND kind = 'tts'", uid)
    assert tts and all(t["chars"] and abs(t["audio_s"] - 0.5) < 0.01 for t in tts)


def test_capture_shaping_is_recorded(env):
    h, uid = member(env)
    with env.websocket_connect("/api/v1/beam", headers=h) as ws:
        ws.send_text(json.dumps({"type": "hello", "device": "P1"}))
        ws.receive_text()
        ws.send_text(json.dumps({"type": "turn.begin", "turn": 4, "mode": "capture", "id": "20261022-0330-5eb8-4",
                                 "started": 1792639812, "tz": "UTC", "text": "A thought"}))
        assert _drain(ws)[-1]["type"] == "done"
    (r,) = rows("SELECT * FROM usage_events WHERE user_id = ?", uid)
    assert (r["kind"], r["workflow"]) == ("turn", "capture")


def test_entities_are_metered_too(env):
    import accounts
    accounts.create_account("rabble-e", "e@example.com", None, kind="entity", tier="entity", api_key="rbbl_entitykey")
    say(env, {"Authorization": "Bearer rbbl_entitykey"})
    r = rows("SELECT u.tier, e.kind FROM usage_events e JOIN users u ON u.id = e.user_id")
    assert r == [{"tier": "entity", "kind": "turn"}]
    assert usage.limits_for("x", "entity")["limits"]["daily_turns"] == 1000


def test_streaming_chat_and_sessions_are_recorded(env, monkeypatch):
    h, uid = member(env)
    body = {"messages": [{"role": "user", "content": "hello"}], "model_tier": "fast"}
    with env.stream("POST", "/api/v1/chat", json=body, headers=h) as r:
        assert r.status_code == 200
        r.read()
    sid = env.post("/api/v1/sessions", json={"resume": True}, headers=h).json()["session_id"]
    with env.stream("POST", f"/api/v1/sessions/{sid}/message", json={"content": "yo", "model_tier": "fast"}, headers=h) as r:
        r.read()
    got = rows("SELECT workflow, tokens_in, status FROM usage_events WHERE user_id = ? ORDER BY ts", uid)
    assert [(g["tokens_in"], g["status"]) for g in got] == [(11, 200), (11, 200)]
    assert got[1]["workflow"] == "session"


# ── limits ───────────────────────────────────────────────────────────────────

def test_admin_is_unlimited_but_still_recorded(env):
    h, uid = member(env, "markp", tier="admin")
    h = {"Authorization": "Bearer " + signup_token(env, "markp")}
    for _ in range(3):
        assert chat(env, h).status_code == 200
    assert usage.limits_for(uid, "admin")["limits"] == {f: -1 for f in usage.FIELDS}
    assert len(rows("SELECT * FROM usage_events WHERE user_id = ?", uid)) == 3
    s = env.get("/api/v1/usage", headers=h).json()
    assert s["totals"]["turns"] == 3 and s["remaining"]["daily_turns"] is None


def signup_token(c, name):
    return c.post("/api/v1/auth/login", json={"identifier": name, "password": PW}).json()["token"]


def test_an_admins_paired_pocket_is_unrestricted_too(env, monkeypatch):
    monkeypatch.setenv("USAGE_PRO_DAILY_TURNS", "0")
    h, uid = member(env, "markp", tier="admin")
    key = devices.create(uid, "devboard")["key"]            # a device key carries tier pro for access...
    say(env, {"Authorization": f"Bearer {key}"})
    assert rows("SELECT kind FROM usage_events WHERE user_id = ?", uid) == [{"kind": "turn"}]   # ...but meters as its owner: admin


def test_limit_enforced_before_the_model_call_on_http(env, monkeypatch):
    monkeypatch.setenv("USAGE_FREE_DAILY_TURNS", "2")
    h, uid = member(env)
    assert [chat(env, h).status_code for _ in range(2)] == [200, 200]
    r = chat(env, h)
    assert r.status_code == 429 and "Usage limit reached" in r.json()["detail"] and "free plan" in r.json()["detail"]
    assert int(r.headers["Retry-After"]) > 0
    assert env.calls["complete"] == 2                          # refused before the model was called
    assert len(rows("SELECT * FROM usage_events WHERE user_id = ?", uid)) == 2      # a refusal is not a turn


def test_limit_enforced_on_the_beam_with_a_clear_error(env, monkeypatch):
    monkeypatch.setenv("USAGE_FREE_DAILY_TURNS", "1")
    h, uid = member(env)
    assert say(env, h)[-1]["type"] == "done"
    llm_calls = env.calls["llm"]
    msgs = say(env, h, turn=2)
    assert msgs[-1]["type"] == "error" and msgs[-1]["text"] == "USAGE LIMIT REACHED"
    assert "Usage limit reached" in msgs[-1]["reason"]
    assert env.calls["llm"] == llm_calls


def test_audio_limit_stops_speech_to_text_before_the_call(env, monkeypatch):
    monkeypatch.setenv("USAGE_FREE_DAILY_AUDIO_S", "2")
    h, uid = member(env)
    assert voice(env, h, secs=1.5)[-1]["type"] == "done"
    msgs = voice(env, h, secs=1.5, turn=2)                       # 1.5 used + 1.5 more is over 2
    assert msgs[-1]["text"] == "USAGE LIMIT REACHED"
    assert len(rows("SELECT * FROM usage_events WHERE user_id = ? AND kind = 'stt'", uid)) == 1


def test_over_the_audio_limit_the_reply_stays_text(env, monkeypatch):
    monkeypatch.setenv("USAGE_FREE_DAILY_AUDIO_S", "1")
    h, uid = member(env)
    usage.record(uid, "tts", audio_s=5.0)
    assert say(env, h)[-1]["type"] == "done"                     # the turn still answers, in text only
    assert rows("SELECT * FROM usage_events WHERE user_id = ? AND kind = 'tts' AND chars IS NOT NULL", uid) == []


def test_override_per_account_then_back_to_default(env, monkeypatch):
    monkeypatch.setenv("USAGE_FREE_DAILY_TURNS", "1")
    _, root = member(env, "markp", tier="admin")
    root_h = {"Authorization": "Bearer " + signup_token(env, "markp")}
    h, uid = member(env, "bob")
    assert chat(env, h).status_code == 200 and chat(env, h).status_code == 429
    r = env.put(f"/api/v1/admin/users/{uid}/limits", json={"daily_turns": 3, "note": "beta tester"}, headers=root_h)
    assert r.status_code == 200 and r.json()["limits"]["daily_turns"] == 3 and r.json()["source"]["daily_turns"] == "account"
    assert r.json()["defaults"]["daily_turns"] == 1 and r.json()["note"] == "beta tester"
    assert chat(env, h).status_code == 200                               # the override applies at once
    env.put(f"/api/v1/admin/users/{uid}/limits", json={"daily_turns": -1}, headers=root_h)
    assert [chat(env, h).status_code for _ in range(3)] == [200, 200, 200]      # unlimited
    env.put(f"/api/v1/admin/users/{uid}/limits", json={"daily_turns": None}, headers=root_h)
    assert chat(env, h).status_code == 429                               # back to the tier default (1/day)
    got = env.get(f"/api/v1/admin/users/{uid}/limits", headers=root_h).json()
    assert got["own"]["daily_turns"] is None and got["limits"]["daily_turns"] == 1
    # fields not sent stay put
    env.put(f"/api/v1/admin/users/{uid}/limits", json={"monthly_turns": 9}, headers=root_h)
    env.put(f"/api/v1/admin/users/{uid}/limits", json={"daily_audio_s": 30}, headers=root_h)
    got = env.get(f"/api/v1/admin/users/{uid}/limits", headers=root_h).json()["own"]
    assert (got["monthly_turns"], got["daily_audio_s"], got["daily_turns"]) == (9, 30, None)


def test_limit_validation_and_unknown_account(env):
    h, _ = member(env, "markp", tier="admin")
    h = {"Authorization": "Bearer " + signup_token(env, "markp")}
    _, uid = member(env, "bob")
    assert env.put(f"/api/v1/admin/users/{uid}/limits", json={"daily_turns": -2}, headers=h).status_code == 422
    assert env.put(f"/api/v1/admin/users/{uid}/limits", json={"daily_turns": "many"}, headers=h).status_code == 422
    assert env.put("/api/v1/admin/users/nobody/limits", json={"daily_turns": 1}, headers=h).status_code == 404
    assert env.get("/api/v1/admin/users/nobody/usage", headers=h).status_code == 404


def test_tier_defaults_from_env_json_and_vars(monkeypatch):
    monkeypatch.setenv("USAGE_DEFAULTS", json.dumps({"free": {"daily_turns": 7, "monthly_turns": 70}, "pro": {"daily_turns": 9}}))
    monkeypatch.setenv("USAGE_FREE_DAILY_TURNS", "5")
    assert usage.tier_defaults("free")["daily_turns"] == 5                # the variable beats the JSON
    assert usage.tier_defaults("free")["monthly_turns"] == 70
    assert usage.tier_defaults("pro")["daily_turns"] == 9 and usage.tier_defaults("pro")["monthly_turns"] == 10000
    assert usage.tier_defaults("nonsense") == usage.tier_defaults("free")      # an unknown tier is a free one
    assert usage.tier_defaults("admin") == {f: -1 for f in usage.FIELDS}
    monkeypatch.setenv("USAGE_DEFAULTS", "not json")
    monkeypatch.delenv("USAGE_FREE_DAILY_TURNS")
    assert usage.tier_defaults("free")["daily_turns"] == 100              # bad JSON is ignored


def test_failed_calls_do_not_count_against_the_limit(env, monkeypatch):
    monkeypatch.setenv("USAGE_FREE_DAILY_TURNS", "1")
    h, uid = member(env)
    usage.record(uid, "turn", status=502)
    assert chat(env, h).status_code == 200


def test_the_web_demo_guest_is_recorded_and_left_unlimited(env, monkeypatch):
    monkeypatch.setenv("USAGE_FREE_DAILY_TURNS", "0")
    import auth
    monkeypatch.setattr(auth, "DEMO_MODE", True)
    monkeypatch.setattr(auth, "DEMO_MODE_PUBLIC", True)
    assert chat(env, {}).status_code == 200
    assert rows("SELECT user_id FROM usage_events") == [{"user_id": "guest"}]


# ── routes and scoping ───────────────────────────────────────────────────────

def test_usage_route_is_own_data_only(env):
    a, ua = member(env, "alice")
    b, ub = member(env, "bob")
    chat(env, a)
    chat(env, a)
    chat(env, b)
    sa = env.get("/api/v1/usage?days=7", headers=a).json()
    assert sa["user_id"] == ua and sa["totals"]["turns"] == 2 and sa["by_kind"]["turn"]["tokens_in"] == 40
    assert sa["by_day"][0]["turns"] == 2 and sa["limits"]["daily_turns"] == 100
    assert sa["remaining"]["daily_turns"] == 98 and sa["used"]["daily_turns"] == 2
    assert env.get("/api/v1/usage", headers=b).json()["totals"]["turns"] == 1
    assert env.get("/api/v1/usage").status_code == 401


def test_admin_routes_are_admin_only_and_never_for_device_keys(env):
    a, ua = member(env, "alice")
    key = devices.create(ua, "P")["key"]
    root, _ = member(env, "markp", tier="admin")
    root = {"Authorization": "Bearer " + signup_token(env, "markp")}
    for path in ("/admin/usage", f"/admin/users/{ua}/usage", f"/admin/users/{ua}/limits", "/admin/events"):
        assert env.get("/api/v1" + path, headers=a).status_code == 403
        assert env.get("/api/v1" + path, headers={"X-API-Key": key}).status_code == 403
        assert env.get("/api/v1" + path).status_code in (401, 403)
        assert env.get("/api/v1" + path, headers=root).status_code == 200
    assert env.put(f"/api/v1/admin/users/{ua}/limits", json={"daily_turns": 1}, headers=a).status_code == 403
    assert env.put(f"/api/v1/admin/users/{ua}/limits", json={"daily_turns": 1}, headers={"X-API-Key": key}).status_code == 403


def test_admin_usage_lists_every_account_and_top_users(env):
    a, ua = member(env, "alice")
    b, ub = member(env, "bob")
    for _ in range(3):
        chat(env, a)
    chat(env, b)
    root = {"Authorization": "Bearer " + signup_token(env, "markp")} if member(env, "markp", tier="admin") else None
    got = env.get("/api/v1/admin/usage?days=30", headers=root).json()
    per = {x["handle"]: x for x in got["accounts"]}
    assert per["alice"]["turns"] == 3 and per["bob"]["turns"] == 1 and per["alice"]["tier"] == "free"
    assert got["top_users"][0]["handle"] == "alice" and got["totals"]["turns"] == 4
    one = env.get(f"/api/v1/admin/users/{ub}/usage", headers=root).json()
    assert one["handle"] == "bob" and one["totals"]["turns"] == 1


# ── account events ───────────────────────────────────────────────────────────

def events(user_id=None):
    return [(e["event"], e["user_id"]) for e in reversed(account_events.recent(user_id, 500))]


def test_signup_login_logout_events_without_secrets(env):
    tok = users.create_invite(created_by="t").token
    r = env.post("/api/v1/auth/signup", json={"username": "alice", "email": "alice@example.com",
                                              "password": PW, "invite_token": tok})
    assert r.status_code == 201
    bad = env.post("/api/v1/auth/login", json={"identifier": "alice", "password": "wrong password here"})
    assert bad.status_code == 401
    env.post("/api/v1/auth/login", json={"identifier": "nobody@example.com", "password": "wrong password here"})
    ok = env.post("/api/v1/auth/login", json={"identifier": "alice", "password": PW})
    env.post("/api/v1/auth/logout")
    uid = ok.json()["user_id"]
    ev = [e for e in reversed(account_events.recent(None, 100))]
    names = [e["event"] for e in ev]
    assert names == ["signup", "invite.redeem", "login.failure", "login.failure", "login.success", "logout"]
    assert ev[0]["user_id"] == uid and ev[2]["user_id"] == uid and ev[3]["user_id"] is None   # unknown account: no id
    assert all(e["ip_hash"] and len(e["ip_hash"]) == 16 for e in ev)
    blob = json.dumps(ev)
    assert "wrong password" not in blob and PW not in blob and "nobody@example.com" not in blob and "testclient" not in blob


def test_verify_reset_and_logout_with_a_cookie_are_logged(env, monkeypatch):
    sent = []

    class Capture(mailer.ConsoleMailer):
        def send(self, to, subject, body):
            sent.append(body)
    mailer.set_mailer(Capture())
    import accounts
    acct = accounts.create_account("carol", "carol@example.com", PW)
    uid = acct["user_id"]
    raw = accounts.issue_token(uid, "verify")
    assert env.post("/api/v1/auth/verify-email", json={"token": raw}).status_code == 200
    assert env.post("/api/v1/auth/forgot-password", json={"email": "carol@example.com"}).status_code == 200
    token = sent[-1].split("token=")[1].split()[0]
    assert env.post("/api/v1/auth/reset-password", json={"token": token, "new_password": "another long phrase 9"}).status_code == 200
    r = env.post("/api/v1/auth/login", json={"identifier": "carol", "password": "another long phrase 9"})
    assert r.status_code == 200
    cookie = r.headers["set-cookie"].split(";")[0].split("=", 1)[1]    # the secure cookie is not kept over http
    env.post("/api/v1/auth/logout", cookies={"rabble_refresh": cookie})   # it names the account
    assert events(uid) == [("email.verify", uid), ("password.reset_requested", uid), ("password.reset_completed", uid),
                           ("login.success", uid), ("logout", uid)]


def test_device_invite_limit_events(env):
    root, rid = member(env, "markp", tier="admin")
    root = {"Authorization": "Bearer " + signup_token(env, "markp")}
    a, ua = member(env, "alice")
    d = env.post("/api/v1/devices", json={"label": "P"}, headers=a).json()
    env.delete(f"/api/v1/devices/{d['id']}", headers=a)
    env.post("/api/v1/admin/invites", json={}, headers=root)
    env.put(f"/api/v1/admin/users/{ua}/limits", json={"daily_turns": 2}, headers=root)
    names = {e["event"] for e in account_events.recent(None, 200)}
    assert {"device.create", "device.revoke", "admin.invite.create", "limit.set", "invite.redeem"} <= names
    le = [e for e in account_events.recent(ua, 50) if e["event"] == "limit.set"][0]
    detail = json.loads(le["detail"])
    assert detail["set"] == {"daily_turns": 2} and detail["by"] == rid and detail["after"]["daily_turns"] == 2


def test_hitting_a_usage_limit_is_logged_once_a_minute(env, monkeypatch):
    monkeypatch.setenv("USAGE_FREE_DAILY_TURNS", "1")
    h, uid = member(env)
    chat(env, h)
    for _ in range(3):
        assert chat(env, h).status_code == 429
    hits = [e for e in account_events.recent(uid, 50) if e["event"] == "usage.limit"]
    assert len(hits) == 1 and "turn" in hits[0]["detail"]


def test_guest_limit_hit_is_logged(env, monkeypatch):
    monkeypatch.setenv("GUEST_PER_GUEST_DAILY_TURNS", "1")
    profile, key = guests.create_guest("g")
    assert guests.spend(profile.id) is None
    assert guests.spend(profile.id) == "daily guest allowance used up"
    assert [e["event"] for e in account_events.recent(profile.id)] == ["guest.limit"]


def test_events_route_filters_and_limits(env):
    root, _ = member(env, "markp", tier="admin")
    root = {"Authorization": "Bearer " + signup_token(env, "markp")}
    a, ua = member(env, "alice")
    got = env.get(f"/api/v1/admin/events?user_id={ua}&limit=1", headers=root).json()["events"]
    assert len(got) == 1 and got[0]["user_id"] == ua
    assert len(env.get("/api/v1/admin/events", headers=root).json()["events"]) > 1
    assert env.get("/api/v1/admin/events?limit=9999", headers=root).status_code == 422


def test_audit_mirrors_account_actions_but_not_model_calls(env):
    audit.log("u1", "device.create", 201, tier="free", resource_id="d1")
    audit.log("u1", "chat", 200, tier="fast")
    audit.log("u1", "beam", 200, tier="fast", workflow_type="voice")
    assert [e["event"] for e in account_events.recent("u1")] == ["device.create"]


# ── logging failures never break a request ───────────────────────────────────

def test_usage_and_event_logging_failures_do_not_break_requests(env, monkeypatch):
    h, uid = member(env)
    real = db.execute

    def broken(sql, params=(), **kw):
        if "usage_events" in sql or "account_events" in sql:
            raise RuntimeError("db down")
        return real(sql, params, **kw)
    monkeypatch.setattr(db, "execute", broken)
    assert chat(env, h).status_code == 200
    assert say(env, h)[-1]["type"] == "done"
    assert env.post("/api/v1/auth/login", json={"identifier": "alice", "password": PW}).status_code == 200
    assert env.post("/api/v1/auth/login", json={"identifier": "alice", "password": "bad bad bad bad"}).status_code == 401
    audit.log("u", "device.create", 201)            # the audit mirror too
    assert usage.record(uid, "turn") is False and account_events.record(uid, "x") is False


def test_a_failing_limit_check_lets_the_call_through(env, monkeypatch):
    h, uid = member(env)

    def boom(*a, **k):
        raise RuntimeError("db down")
    monkeypatch.setattr(usage, "refusal", boom)
    assert chat(env, h).status_code == 200


# ── store level, SQLite and Postgres ─────────────────────────────────────────

@pytest.fixture()
def store(backend, data_dir):
    db.conn()
    usage._last_hit.clear()
    return backend


def test_store_record_check_summary_and_limits(store, monkeypatch):
    monkeypatch.setenv("USAGE_FREE_DAILY_TURNS", "2")
    import accounts
    uid = accounts.create_account("dana", "d@example.com", PW)["user_id"]
    user = {"sub": uid, "tier": "free"}
    assert usage.record(user, "turn", workflow="chat", provider="groq", model="m", tokens_in=10, tokens_out=4, ms=12.6)
    assert usage.record(user, "stt", audio_s=3.25, status=200, device="P1")
    assert usage.record(user, "tts", audio_s=1.0, chars=40)
    usage.check(user, "turn")                                  # one of two used
    usage.record(user, "turn", status=200)
    with pytest.raises(Exception) as hit:
        usage.check(user, "turn")
    assert hit.value.status_code == 429
    s = usage.summary(uid, 30)
    assert s["totals"]["turns"] == 2 and s["totals"]["tokens_in"] == 10
    assert abs(s["totals"]["audio_s"] - 4.25) < 0.1 and abs(s["by_kind"]["stt"]["audio_s"] - 3.25) < 0.1
    assert s["used"]["daily_turns"] == 2 and s["remaining"]["daily_turns"] == 0
    assert s["by_day"][0]["events"] == 4
    usage.set_limits(uid, {"daily_turns": -1, "daily_audio_s": 4}, "admin", "note")
    usage.check(user, "turn")                                   # unlimited now
    with pytest.raises(Exception):
        usage.check(user, "stt", 1.0)                           # 4.25 used > 4
    usage.set_limits(uid, {"daily_audio_s": None}, "admin")
    usage.check(user, "stt", 1.0)
    assert usage.limits_for(uid, "free")["source"]["daily_audio_s"] == "tier"
    top = usage.admin_summary(30)
    assert top["accounts"][0]["user_id"] == uid and top["accounts"][0]["handle"] == "dana"


def test_store_events_and_export(store):
    assert account_events.record("u1", "login.success", ip_hash="abc", detail={"a": 1})
    usage.record("u1", "turn")
    usage.set_limits("u1", {"daily_turns": 5}, "admin")
    out = db.export_json()
    assert set(out) == set(db.EXPORT_TABLES)
    for t in ("pocket_conversations", "pocket_turns", "pocket_notes", "usage_events", "account_limits", "account_events"):
        assert t in db.EXPORT_TABLES
    assert out["account_events"][0]["detail"] == '{"a": 1}' and len(out["usage_events"]) == 1
    assert out["account_limits"][0]["daily_turns"] == 5


# ── snapshot merge (SQLite) ──────────────────────────────────────────────────

def test_late_restore_merges_pocket_usage_and_event_rows(data_dir):
    import accounts
    import pocket_store
    uid = accounts.create_account("erin", "e@example.com", PW)["user_id"]
    snap = data_dir / "snap.db"
    db.backup(snap)                                            # the snapshot: an account and nothing else
    pocket_store.save_note(uid, "n1", "P", "T", "text", "2026-11-17", "---\n---\nbody", 1.0)
    cid = pocket_store.append_turns(uid, "P", [("user", "hi"), ("assistant", "yo")])
    usage.record(uid, "turn", workflow="voice")
    account_events.record(uid, "login.success")
    account_events.record(None, "login.failure")
    usage.set_limits(uid, {"daily_turns": 7}, "admin")
    db.replace_with(snap.read_bytes(), merge_local=True)
    assert [r["id"] for r in db.query("SELECT id FROM pocket_notes")] == ["n1"]
    assert len(db.query("SELECT * FROM pocket_turns WHERE conversation_id = ?", (cid,))) == 2
    assert len(db.query("SELECT * FROM usage_events")) == 1
    assert sorted(r["event"] for r in db.query("SELECT event FROM account_events")) == ["login.failure", "login.success"]
    assert db.query_one("SELECT daily_turns FROM account_limits")["daily_turns"] == 7


# ── token counts from the providers (llm.py sink) ────────────────────────────

def _mock_llm(monkeypatch, handler):
    import httpx
    import llm

    class Client(httpx.AsyncClient):
        def __init__(self, *a, **k):
            super().__init__(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(llm.httpx, "AsyncClient", Client)
    monkeypatch.setenv("GROQ_API_KEY", "k")
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")


def _sse(*chunks):
    return ("".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n").encode()


def test_llm_sink_reads_groq_stream_usage_and_openrouter_usage(monkeypatch):
    import asyncio
    import httpx
    import llm
    seen = []

    def handler(request):
        body = json.loads(request.content)
        seen.append(body)
        usage_chunk = {"choices": [], "usage": {"prompt_tokens": 31, "completion_tokens": 9}}
        if "groq" in str(request.url):
            usage_chunk = {"choices": [{"delta": {}}], "x_groq": {"usage": {"prompt_tokens": 12, "completion_tokens": 3}}}
        return httpx.Response(200, content=_sse({"choices": [{"delta": {"content": "hey"}}]}, usage_chunk))
    _mock_llm(monkeypatch, handler)

    async def run(provider):
        sink = {}
        out = [c async for c in llm.stream_chat([{"role": "user", "content": "hi"}], "s", model_tier="fast",
                                                chain=[{"provider": provider, "model": "m1"}], sink=sink)]
        return out, sink
    out, sink = asyncio.run(run("groq"))
    assert out == ["hey"] and sink == {"provider": "groq", "model": "m1", "tokens_in": 12, "tokens_out": 3}
    assert "stream_options" not in seen[-1]
    out, sink = asyncio.run(run("openrouter"))
    assert sink == {"provider": "openrouter", "model": "m1", "tokens_in": 31, "tokens_out": 9}
    assert seen[-1]["stream_options"] == {"include_usage": True}


def test_llm_sink_on_complete_chat_and_a_provider_that_reports_nothing(monkeypatch):
    import asyncio
    import httpx
    import llm

    def handler(request):
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})
    _mock_llm(monkeypatch, handler)
    sink = {}
    monkeypatch.setattr(llm, "_resolve_candidates", lambda tier, chain, user: [{"provider": "groq", "model": "m2"}])
    assert asyncio.run(llm.complete_chat([{"role": "user", "content": "x"}], "s", model_tier="fast", sink=sink)) == "ok"
    assert sink == {"provider": "groq", "model": "m2"}            # no usage block: no token numbers, nothing invented
