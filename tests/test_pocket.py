"""Pocket notes and conversations kept per member account (pocket_store.py, pocket_routes.py, beam.py). No network."""

import json

import pytest
from fastapi.testclient import TestClient

import beam
import db
import guests
import mailer
import pocket_store
import rate_limit
import users

PW = "correct horse battery"


@pytest.fixture()
def env(data_dir, monkeypatch):
    for k in ("ACCOUNT_CAP", "REQUIRE_VERIFIED_EMAIL", "AUTH_IP_HEADER", "MAIL_PROVIDER", "SIGNUP_INVITE_ONLY",
              "RABBLE_ADMIN_KEY", "BEAM_DEVICE_TOKEN", "BEAM_ALLOW_GUEST", "BEAM_ALLOW_GUEST_POCKETS"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("DEMO_MODE", "false")
    rate_limit._windows.clear()
    mailer.set_mailer(mailer.ConsoleMailer())
    beam._kept.clear()
    beam._history.clear()

    async def fake_stream(messages, system, **kw):
        if "tidy note" in system:
            yield json.dumps({"title": "Backplate ideas", "tags": [], "summary": [], "notes": "Tight."})
        else:
            yield "Hello "
            yield "there."
    monkeypatch.setattr(beam.llm, "stream_chat", fake_stream)
    import main
    yield TestClient(main.app)
    mailer.set_mailer(None)
    rate_limit._windows.clear()
    beam._kept.clear()
    beam._history.clear()


def member(c, name="alice"):
    tok = users.create_invite(created_by="t").token
    assert c.post("/api/v1/auth/signup", json={"username": name, "email": f"{name}@example.com",
                                               "password": PW, "invite_token": tok}).status_code == 201
    t = c.post("/api/v1/auth/login", json={"identifier": name, "password": PW}).json()["token"]
    return {"Authorization": f"Bearer {t}"}


def _drain(ws, until="done"):
    msgs = []
    while True:
        m = json.loads(ws.receive_text())
        msgs.append(m)
        if m["type"] == until or m["type"] == "error":
            return msgs


def say(c, headers, text="hi pocket", device="P1", turn=1):
    with c.websocket_connect("/api/v1/beam", headers=headers) as ws:
        ws.send_text(json.dumps({"type": "hello", "device": device}))
        assert json.loads(ws.receive_text())["type"] == "ready"
        ws.send_text(json.dumps({"type": "turn.begin", "turn": turn, "mode": "text", "text": text}))
        return _drain(ws)


def capture(c, headers, text="Backplate tolerance is tight", note_id="20261022-0330-5eb8-4", device="P1"):
    with c.websocket_connect("/api/v1/beam", headers=headers) as ws:
        ws.send_text(json.dumps({"type": "hello", "device": device}))
        ws.receive_text()
        ws.send_text(json.dumps({"type": "turn.begin", "turn": 4, "mode": "capture", "id": note_id,
                                 "started": 1792639812, "tz": "America/New_York", "text": text}))
        return _drain(ws)


def test_member_note_and_conversation_persist(env):
    h = member(env)
    capture(env, h)
    say(env, h, "what time is it")
    notes = env.get("/api/v1/pocket/notes", headers=h).json()["notes"]
    assert len(notes) == 1
    n = notes[0]
    assert set(n) == {"id", "title", "source", "created", "device", "ts", "preview"}
    assert n["id"] == "20261022-0330-5eb8-4" and n["title"] == "Backplate ideas" and n["source"] == "text"
    assert n["device"] == "P1" and n["created"].startswith("2026-10-21T23:30:12")
    assert n["preview"].startswith("## Notes") and "---" not in n["preview"][:5]
    full = env.get(f"/api/v1/pocket/notes/{n['id']}", headers=h).json()
    assert set(full) == {"id", "title", "source", "created", "device", "ts", "md"} and full["md"].startswith("---\n")
    convos = env.get("/api/v1/pocket/conversations", headers=h).json()["conversations"]
    assert len(convos) == 1 and set(convos[0]) == {"id", "device", "started_at", "ended_at", "turns", "preview"}
    assert convos[0]["turns"] == 2 and convos[0]["preview"] == "what time is it" and convos[0]["ended_at"] is None
    one = env.get(f"/api/v1/pocket/conversations/{convos[0]['id']}", headers=h).json()
    assert [(t["role"], t["text"]) for t in one["turns"]] == [("user", "what time is it"), ("assistant", "Hello there.")]


def test_note_upsert_by_id(env):
    h = member(env)
    capture(env, h, text="first")
    capture(env, h, text="second")
    notes = env.get("/api/v1/pocket/notes", headers=h).json()["notes"]
    assert len(notes) == 1
    assert "second" in env.get(f"/api/v1/pocket/notes/{notes[0]['id']}", headers=h).json()["md"]


def test_turns_append_to_one_open_conversation(env):
    h = member(env)
    say(env, h, "one", turn=1)
    say(env, h, "two", turn=2)
    convos = env.get("/api/v1/pocket/conversations", headers=h).json()["conversations"]
    assert len(convos) == 1 and convos[0]["turns"] == 4


def test_guest_shared_pocket_and_entity_not_persisted(env, monkeypatch):
    monkeypatch.setenv("BEAM_ALLOW_GUEST_POCKETS", "true")   # guest and shared-token Pockets are test-only
    # per-device guest (tier guest)
    profile, key = guests.create_guest("g")
    say(env, {"Authorization": f"Bearer {key}"})
    capture(env, {"Authorization": f"Bearer {key}"})
    # the shared BEAM_DEVICE_TOKEN user
    monkeypatch.setenv("BEAM_DEVICE_TOKEN", "shared-secret")
    say(env, {"Authorization": "Bearer shared-secret"})
    capture(env, {"Authorization": "Bearer shared-secret"})
    # bench guest (BEAM_ALLOW_GUEST)
    monkeypatch.setenv("BEAM_ALLOW_GUEST", "true")
    say(env, {})
    # an entity account (non-human, uses its api key)
    import accounts
    accounts.create_account("rabble-e", "e@example.com", None, kind="entity", tier="entity", api_key="rbbl_entitykey")
    ukey = "rbbl_entitykey"
    say(env, {"Authorization": f"Bearer {ukey}"})
    capture(env, {"Authorization": f"Bearer {ukey}"})
    assert db.query_one("SELECT COUNT(*) AS n FROM pocket_notes")["n"] == 0
    assert db.query_one("SELECT COUNT(*) AS n FROM pocket_conversations")["n"] == 0
    assert db.query_one("SELECT COUNT(*) AS n FROM pocket_turns")["n"] == 0


def test_routes_refuse_non_members(env):
    assert env.get("/api/v1/pocket/notes").status_code == 401
    assert env.get("/api/v1/pocket/conversations").status_code == 401
    _, key = guests.create_guest("g")
    assert env.get("/api/v1/pocket/notes", headers={"X-API-Key": key}).status_code == 401


def test_member_device_key_reads_and_writes(env):
    h = member(env)
    capture(env, h)
    import devices
    uid = db.query_one("SELECT user_id FROM pocket_notes")["user_id"]
    key = devices.create(uid, "Pocket")["key"]        # a member's usable api key is a device key
    assert len(env.get("/api/v1/pocket/notes", headers={"X-API-Key": key}).json()["notes"]) == 1


def test_scoping_between_users(env):
    a, b = member(env, "alice"), member(env, "bob")
    capture(env, a)
    say(env, a)
    nid = env.get("/api/v1/pocket/notes", headers=a).json()["notes"][0]["id"]
    cid = env.get("/api/v1/pocket/conversations", headers=a).json()["conversations"][0]["id"]
    assert env.get("/api/v1/pocket/notes", headers=b).json() == {"notes": []}
    assert env.get("/api/v1/pocket/conversations", headers=b).json() == {"conversations": []}
    for path in (f"notes/{nid}", f"conversations/{cid}"):
        assert env.get(f"/api/v1/pocket/{path}", headers=b).status_code == 404
        assert env.delete(f"/api/v1/pocket/{path}", headers=b).status_code == 404
    assert env.get(f"/api/v1/pocket/notes/{nid}", headers=a).status_code == 200      # untouched
    assert env.get(f"/api/v1/pocket/conversations/{cid}", headers=a).status_code == 200
    # the same note id for two users does not collide
    capture(env, b)
    assert len(env.get("/api/v1/pocket/notes", headers=b).json()["notes"]) == 1


def test_delete_and_404(env):
    h = member(env)
    capture(env, h)
    say(env, h)
    nid = env.get("/api/v1/pocket/notes", headers=h).json()["notes"][0]["id"]
    cid = env.get("/api/v1/pocket/conversations", headers=h).json()["conversations"][0]["id"]
    assert env.delete(f"/api/v1/pocket/notes/{nid}", headers=h).json() == {"ok": True}
    assert env.delete(f"/api/v1/pocket/conversations/{cid}", headers=h).json() == {"ok": True}
    assert env.get(f"/api/v1/pocket/notes/{nid}", headers=h).status_code == 404
    assert env.get(f"/api/v1/pocket/conversations/{cid}", headers=h).status_code == 404
    assert env.delete(f"/api/v1/pocket/notes/{nid}", headers=h).status_code == 404
    assert db.query_one("SELECT COUNT(*) AS n FROM pocket_turns")["n"] == 0


def test_paging_limit_and_before(env):
    h = member(env)
    for i in range(3):
        capture(env, h, text=f"n{i}", note_id=f"20261022-0330-5eb8-{i}")
    got = env.get("/api/v1/pocket/notes?limit=2", headers=h).json()["notes"]
    assert [n["id"] for n in got] == ["20261022-0330-5eb8-2", "20261022-0330-5eb8-1"]
    rest = env.get("/api/v1/pocket/notes", params={"before": got[-1]["ts"]}, headers=h).json()["notes"]
    assert [n["id"] for n in rest] == ["20261022-0330-5eb8-0"]
    assert env.get("/api/v1/pocket/notes?before=nonsense", headers=h).status_code == 422


def test_new_convo_clears_history_and_rotates(env):
    h = member(env)
    say(env, h, "before")
    key = next(iter(beam._history))
    assert len(beam._history[key]) == 2
    first = env.get("/api/v1/pocket/conversations", headers=h).json()["conversations"][0]["id"]
    with env.websocket_connect("/api/v1/beam", headers=h) as ws:
        ws.send_text(json.dumps({"type": "hello", "device": "P1"}))
        ws.receive_text()
        ws.send_text(json.dumps({"type": "new_convo"}))
        reply = json.loads(ws.receive_text())
        ws.send_text(json.dumps({"type": "new_convo"}))
        again = json.loads(ws.receive_text())
    assert reply["type"] == "convo_new" and reply["id"] and reply["id"] != first
    assert again["id"] == reply["id"]                       # an empty open conversation is reused
    assert key not in beam._history
    say(env, h, "after")
    convos = env.get("/api/v1/pocket/conversations", headers=h).json()["conversations"]
    assert [c["id"] for c in convos] == [reply["id"], first]  # the empty one is not listed until it has turns
    assert convos[1]["ended_at"] is not None and convos[0]["ended_at"] is None
    assert convos[0]["turns"] == 2 and convos[1]["turns"] == 2


def test_new_convo_for_non_persisted_users(env, monkeypatch):
    monkeypatch.setenv("BEAM_ALLOW_GUEST_POCKETS", "true")
    monkeypatch.setenv("BEAM_DEVICE_TOKEN", "shared-secret")
    h = {"Authorization": "Bearer shared-secret"}
    say(env, h)
    assert "pocket:P1" in beam._history
    with env.websocket_connect("/api/v1/beam", headers=h) as ws:
        ws.send_text(json.dumps({"type": "hello", "device": "P1"}))
        ws.receive_text()
        ws.send_text(json.dumps({"type": "new_convo"}))
        assert json.loads(ws.receive_text()) == {"type": "convo_new", "id": None}
    assert "pocket:P1" not in beam._history


def test_store_failure_does_not_fail_turn_or_capture(env, monkeypatch):
    h = member(env)

    def boom(*a, **k):
        raise RuntimeError("db down")
    monkeypatch.setattr(pocket_store, "append_turns", boom)
    monkeypatch.setattr(pocket_store, "save_note", boom)
    monkeypatch.setattr(pocket_store, "new_conversation", boom)
    msgs = say(env, h)
    assert msgs[-1]["type"] == "done" and len(beam._history) == 1
    msgs = capture(env, h)
    assert msgs[-1]["type"] == "done" and any(m["type"] == "note" for m in msgs)
    beam._kept.clear()    # the unsaved note would be resent on the next hello
    with env.websocket_connect("/api/v1/beam", headers=h) as ws:
        ws.send_text(json.dumps({"type": "hello", "device": "P1"}))
        ws.receive_text()
        ws.send_text(json.dumps({"type": "new_convo"}))
        assert json.loads(ws.receive_text()) == {"type": "convo_new", "id": None}
    assert not beam._history


@pytest.fixture()
def store(backend, data_dir):
    db.conn()
    return backend


def test_store_roundtrip_both_engines(store):
    pocket_store.save_note("u1", "n1", "P", "T", "text", "2026-01-01T00:00:00+00:00", "---\nid: n1\n---\n\nbody", 1.0)
    pocket_store.save_note("u1", "n1", "P", "T2", "voice", "2026-01-01T00:00:00+00:00", "---\n---\nbody2", 2.0)
    assert pocket_store.get_note("u1", "n1")["title"] == "T2" and pocket_store.get_note("u2", "n1") is None
    cid = pocket_store.append_turns("u1", "P", [("user", "a"), ("assistant", "b")])
    assert pocket_store.get_conversation("u1", cid)["turns"][0]["text"] == "a"
    assert pocket_store.get_conversation("u2", cid) is None
    assert pocket_store.new_conversation("u1", "P") != cid
    assert pocket_store.list_conversations("u1", 10, None)[0]["turns"] == 2
    assert pocket_store.delete_conversation("u1", cid) and pocket_store.get_conversation("u1", cid) is None
    assert pocket_store.delete_note("u1", "n1") and not pocket_store.delete_note("u1", "n1")
