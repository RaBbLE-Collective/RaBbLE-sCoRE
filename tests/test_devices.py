"""Device keys (AC7): members mint keys for their Pockets; a key resolves to its owner."""

import asyncio
import json

import pytest
from fastapi.testclient import TestClient

import accounts
import beam
import db
import devices
import mailer
import rate_limit
import users

PW = "correct horse battery"


@pytest.fixture()
def env(data_dir, monkeypatch):
    for k in ("ACCOUNT_CAP", "REQUIRE_VERIFIED_EMAIL", "AUTH_IP_HEADER", "MAIL_PROVIDER", "SIGNUP_INVITE_ONLY",
              "RABBLE_ADMIN_KEY", "DEVICE_LIMIT", "BEAM_DEVICE_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("DEMO_MODE", "false")
    rate_limit._windows.clear()
    devices._touched.clear()
    mailer.set_mailer(mailer.ConsoleMailer())
    import main
    yield TestClient(main.app)
    mailer.set_mailer(None)
    rate_limit._windows.clear()
    devices._touched.clear()


def member(c, name="alice"):
    tok = users.create_invite(created_by="t").token
    r = c.post("/api/v1/auth/signup", json={"username": name, "email": f"{name}@example.com",
                                            "password": PW, "invite_token": tok})
    assert r.status_code == 201
    t = c.post("/api/v1/auth/login", json={"identifier": name, "password": PW}).json()["token"]
    return {"Authorization": f"Bearer {t}"}


def mint(c, h, label="Pocket"):
    return c.post("/api/v1/devices", json={"label": label}, headers=h)


class _WS:
    def __init__(self, key):
        self.headers = {"authorization": f"Bearer {key}"}


def test_create_list_revoke(env):
    h = member(env)
    r = mint(env, h, "Desk Pocket")
    assert r.status_code == 201
    d = r.json()
    assert set(d) == {"id", "label", "key", "created_at"} and d["key"].startswith("rbdv_") and d["label"] == "Desk Pocket"
    listed = env.get("/api/v1/devices", headers=h).json()["devices"]
    assert len(listed) == 1 and "key" not in listed[0]
    assert set(listed[0]) == {"id", "label", "created_at", "last_seen", "revoked", "fw", "fw_seen", "fp"} and listed[0]["revoked"] is False
    assert listed[0]["fw"] == ""
    import hashlib
    assert listed[0]["fp"] == hashlib.sha256(d["key"].encode()).hexdigest()[:12]   # what the Pocket's status key_fp shows
    assert env.delete(f"/api/v1/devices/{d['id']}", headers=h).status_code == 200
    listed = env.get("/api/v1/devices", headers=h).json()["devices"]
    assert listed == []                                           # revoked Pockets are deleted, not listed
    assert env.delete(f"/api/v1/devices/{d['id']}", headers=h).status_code == 404   # and gone for good


def test_label_validation(env):
    h = member(env)
    assert env.post("/api/v1/devices", json={"label": ""}, headers=h).status_code == 422
    assert env.post("/api/v1/devices", json={"label": "x" * 41}, headers=h).status_code == 422
    assert env.post("/api/v1/devices", json={"label": "   "}, headers=h).status_code == 422


def test_key_shown_once_only_hash_stored(env):
    h = member(env)
    key = mint(env, h).json()["key"]
    assert key not in json.dumps(db.export_json(), default=str)
    assert key not in json.dumps(env.get("/api/v1/devices", headers=h).json())
    row = db.query_one("SELECT key_hash FROM devices")
    assert row["key_hash"] == devices._hash(key)


def test_limit_and_slot_freed_by_revoke(env, monkeypatch):
    h = member(env)
    ids = [mint(env, h, f"p{i}").json()["id"] for i in range(3)]
    r = mint(env, h, "p4")
    assert r.status_code == 409 and "limit" in r.json()["detail"].lower()
    env.delete(f"/api/v1/devices/{ids[0]}", headers=h)
    assert mint(env, h, "p4").status_code == 201
    monkeypatch.setenv("DEVICE_LIMIT", "0")
    assert devices.limit() == 1
    monkeypatch.setenv("DEVICE_LIMIT", "junk")
    assert devices.limit() == 3


def test_mint_is_rate_limited(env, monkeypatch):
    monkeypatch.setenv("DEVICE_LIMIT", "100")
    h = member(env)
    codes = [mint(env, h).status_code for _ in range(11)]
    assert codes[:10] == [201] * 10 and codes[10] == 429


def test_cross_account_isolation(env):
    a, b = member(env, "alice"), member(env, "bobby")
    did = mint(env, a).json()["id"]
    assert env.get("/api/v1/devices", headers=b).json()["devices"] == []
    assert env.delete(f"/api/v1/devices/{did}", headers=b).status_code == 404
    assert env.delete("/api/v1/devices/nope", headers=a).status_code == 404
    assert env.get("/api/v1/devices", headers=a).json()["devices"][0]["revoked"] is False


def test_guest_and_anonymous_refused(env):
    assert env.post("/api/v1/devices", json={"label": "x"}).status_code in (401, 403)
    assert env.get("/api/v1/devices").status_code in (401, 403)
    assert env.delete("/api/v1/devices/x").status_code in (401, 403)
    users.create_user(users.UserProfile(id="g1", handle="g1", display_name="G", join_date="2026-01-01", intention="",
                                        api_key="rbbl_guestkey", tier="guest"))
    r = env.post("/api/v1/devices", json={"label": "x"}, headers={"X-API-Key": "rbbl_guestkey"})
    assert r.status_code == 403


def test_device_key_authenticates_as_owner_http_and_beam(env):
    h = member(env)
    d = mint(env, h).json()
    me = env.get("/api/v1/auth/me", headers={"X-API-Key": d["key"]})
    assert me.status_code == 200
    owner = accounts.by_username("alice")["user_id"]
    assert me.json().get("id") == owner or me.json().get("sub") == owner or owner in json.dumps(me.json())
    claims = beam._authorize(_WS(d["key"]))
    assert claims["sub"] == owner and claims["device_id"] == d["id"] and claims["tier"] == "free"
    from auth_routes import verify_api_key
    v = verify_api_key(d["key"])
    assert v["id"] == owner and v["device_id"] == d["id"] and v["handle"] == "alice"


def test_tier_is_read_live(env):
    h = member(env)
    d = mint(env, h).json()
    uid = accounts.by_username("alice")["user_id"]
    p = users.get_user(uid)
    p.tier = "pro"
    users.save_user(p)
    assert beam._authorize(_WS(d["key"]))["tier"] == "pro"


def test_revoked_unknown_and_guest_owner_refused(env):
    h = member(env)
    d = mint(env, h).json()
    assert beam._authorize(_WS(d["key"])) is not None
    env.delete(f"/api/v1/devices/{d['id']}", headers=h)
    assert beam._authorize(_WS(d["key"])) is None
    assert env.get("/api/v1/auth/me", headers={"X-API-Key": d["key"]}).status_code == 401
    assert env.get("/api/v1/auth/me", headers={"X-API-Key": "rbdv_nonsense"}).status_code == 401
    d2 = mint(env, h).json()
    uid = accounts.by_username("alice")["user_id"]
    p = users.get_user(uid)
    p.tier = "guest"
    users.save_user(p)
    assert beam._authorize(_WS(d2["key"])) is None
    db.execute("DELETE FROM users WHERE id = ?", (uid,))
    assert devices.resolve(d2["key"]) is None


def _admin_owner(env):
    h = member(env, "boss")
    uid = accounts.by_username("boss")["user_id"]
    p = users.get_user(uid)
    p.tier = "admin"
    users.save_user(p)
    h = {"Authorization": "Bearer " + env.post("/api/v1/auth/login", json={"identifier": "boss", "password": PW}).json()["token"]}
    return h


def test_device_key_cannot_reach_admin_even_for_admin_owner(env):
    h = _admin_owner(env)
    assert env.get("/api/v1/admin/invites", headers=h).status_code == 200
    d = mint(env, h).json()
    assert env.get("/api/v1/admin/invites", headers={"X-API-Key": d["key"]}).status_code == 403
    assert env.post("/api/v1/admin/invites", json={}, headers={"X-API-Key": d["key"]}).status_code == 403
    assert beam._authorize(_WS(d["key"]))["tier"] == "pro"


def test_device_key_cannot_mint_or_manage_devices(env):
    h = member(env)
    d = mint(env, h).json()
    k = {"X-API-Key": d["key"]}
    assert env.post("/api/v1/devices", json={"label": "x"}, headers=k).status_code == 403
    assert env.get("/api/v1/devices", headers=k).status_code == 403
    assert env.delete(f"/api/v1/devices/{d['id']}", headers=k).status_code == 403
    assert len(env.get("/api/v1/devices", headers=h).json()["devices"]) == 1


def test_last_seen_throttled(env, monkeypatch):
    h = member(env)
    d = mint(env, h).json()
    t = [1000.0]
    monkeypatch.setattr(devices.time, "monotonic", lambda: t[0])
    monkeypatch.setattr(devices, "_now", lambda: f"stamp{t[0]}")
    assert db.query_one("SELECT last_seen FROM devices")["last_seen"] == ""
    devices.resolve(d["key"])
    assert db.query_one("SELECT last_seen FROM devices")["last_seen"] == "stamp1000.0"
    t[0] = 1030.0
    devices.resolve(d["key"])
    assert db.query_one("SELECT last_seen FROM devices")["last_seen"] == "stamp1000.0"   # within 60 s: no write
    t[0] = 1061.0
    devices.resolve(d["key"])
    assert db.query_one("SELECT last_seen FROM devices")["last_seen"] == "stamp1061.0"


def test_device_key_survives_revoke_all(env):
    """Password reset and logout end sessions, not Pockets: a device key is revoked only by its owner."""
    h = member(env)
    d = mint(env, h).json()
    uid = accounts.by_username("alice")["user_id"]
    accounts.revoke_all(uid)
    assert env.get("/api/v1/auth/me", headers=h).status_code == 401
    assert env.get("/api/v1/auth/me", headers={"X-API-Key": d["key"]}).status_code == 200
    assert beam._authorize(_WS(d["key"]))["sub"] == uid


def test_shared_beam_token_and_guest_keys_refused_by_default(env, monkeypatch):
    import guests
    monkeypatch.delenv("BEAM_ALLOW_GUEST_POCKETS", raising=False)
    monkeypatch.setenv("BEAM_DEVICE_TOKEN", "shared-secret")
    _, gkey = guests.create_guest("g")
    assert beam._authorize(_WS("shared-secret")) is None
    assert beam._authorize(_WS(gkey)) is None
    from auth_routes import verify_api_key
    gtok = auth_make_token(verify_api_key(gkey))
    assert beam._authorize(_WS(gtok)) is None            # a JWT minted from a guest key is refused too


def test_shared_beam_token_and_guest_keys_accepted_with_the_testing_flag(env, monkeypatch):
    import guests
    monkeypatch.setenv("BEAM_ALLOW_GUEST_POCKETS", "true")
    monkeypatch.setenv("BEAM_DEVICE_TOKEN", "shared-secret")
    _, gkey = guests.create_guest("g")
    assert beam._authorize(_WS("shared-secret")) == {"sub": "pocket", "name": "RaBbLE-Pocket", "tier": "pro"}
    got = beam._authorize(_WS(gkey))
    assert got["tier"] == "guest" and got["llm_backend"] == "hosted_groq"


def auth_make_token(user):
    import auth
    return auth.make_token(user["id"], user["name"], user["tier"], user.get("handle", ""))


def test_beam_accepts_only_device_keys_entities_and_member_jwts(env, monkeypatch):
    monkeypatch.delenv("BEAM_ALLOW_GUEST_POCKETS", raising=False)
    monkeypatch.delenv("BEAM_ALLOW_GUEST", raising=False)
    h = member(env)
    uid = accounts.by_username("alice")["user_id"]
    assert beam._authorize(_WS(h["Authorization"].split()[1]))["sub"] == uid          # member JWT
    assert beam._authorize(_WS(mint(env, h).json()["key"]))["sub"] == uid             # device key
    legacy = users.get_user(uid).api_key
    assert beam._authorize(_WS(legacy)) is None                                        # a plain member api key
    accounts.create_account("rabble-e", "e@example.com", None, kind="entity", tier="entity", api_key="rbbl_entitykey")
    assert beam._authorize(_WS("rbbl_entitykey"))["tier"] == "entity"                  # entity keys keep working
    monkeypatch.setattr("auth_routes._ADMIN_KEY", "adminkey")
    assert beam._authorize(_WS("adminkey")) is None                                    # the shared admin key is no account
    assert beam._authorize(_WS("not-a-key")) is None


def test_beam_refusal_closes_with_a_distinct_reason(env, monkeypatch):
    import json as _json
    from starlette.websockets import WebSocketDisconnect
    monkeypatch.delenv("BEAM_ALLOW_GUEST_POCKETS", raising=False)
    monkeypatch.delenv("BEAM_ALLOW_GUEST", raising=False)
    monkeypatch.setenv("BEAM_DEVICE_TOKEN", "shared-secret")
    with pytest.raises(WebSocketDisconnect) as bad:
        with env.websocket_connect("/api/v1/beam", headers={"Authorization": "Bearer shared-secret"}):
            pass
    assert bad.value.code == 4401 and bad.value.reason.startswith("key not accepted")
    with pytest.raises(WebSocketDisconnect) as none:
        with env.websocket_connect("/api/v1/beam"):
            pass
    assert none.value.code == 4401 and none.value.reason == "device key required"


def test_admin_is_exempt_from_the_device_limit_and_members_are_not(env, monkeypatch):
    monkeypatch.setenv("DEVICE_LIMIT", "2")
    h = member(env)
    uid = accounts.by_username("alice")["user_id"]
    assert [mint(env, h, f"p{i}").status_code for i in range(3)] == [201, 201, 409]
    prof = users.get_user(uid)
    prof.tier = "admin"
    users.save_user(prof)
    assert devices.is_exempt(uid) is True
    for i in range(5):
        assert devices.create(uid, f"dev{i}") is not None
    assert devices.active_count(uid) == 7


# ── store level, SQLite and Postgres ──────────────────────────────────────────

@pytest.fixture()
def store(backend, data_dir):
    devices._touched.clear()
    db.conn()
    return backend


def test_store_roundtrip(store):
    a = accounts.create_account("mark", "m@example.com", PW)
    d = devices.create(a["user_id"], "P1")
    assert devices.resolve(d["key"])["id"] == a["user_id"]
    assert devices.list_for(a["user_id"])[0]["last_seen"] != ""
    assert devices.revoke(a["user_id"], d["id"]) is True
    assert devices.resolve(d["key"]) is None
    assert devices.revoke("other", d["id"]) is False
    assert devices.list_for(a["user_id"]) == []
    assert db.export_json()["devices"] == []              # the row is gone from the database, not flagged


def test_firmware_version_is_kept_per_device(store):
    a = accounts.create_account("mark", "m@example.com", PW)
    d = devices.create(a["user_id"], "P1")
    devices.note_firmware(d["id"], "0.1.2")
    row = devices.list_for(a["user_id"])[0]
    assert row["fw"] == "0.1.2" and row["fw_seen"]
    devices.note_firmware(d["id"], "0.1.3-dev.abc1234\r\n<b>x</b>" + "9" * 80)   # junk and length are cut
    assert devices.list_for(a["user_id"])[0]["fw"].startswith("0.1.3-dev.abc1234") and len(devices.list_for(a["user_id"])[0]["fw"]) <= 40
    devices.note_firmware(d["id"], "")                                              # an empty report changes nothing
    assert devices.list_for(a["user_id"])[0]["fw"].startswith("0.1.3-dev")


def test_report_from_the_members_computer(env):
    h = member(env)
    d = mint(env, h, "Desk Pocket").json()
    r = env.post(f"/api/v1/devices/{d['id']}/report", json={"fw": "0.1.2"}, headers=h)
    assert r.status_code == 200
    row = env.get("/api/v1/devices", headers=h).json()["devices"][0]
    assert row["fw"] == "0.1.2" and row["last_seen"]
    assert env.post("/api/v1/devices/nope/report", json={"fw": "1"}, headers=h).status_code == 404
    assert env.post(f"/api/v1/devices/{d['id']}/report", json={"fw": "1"}).status_code == 401   # no session
