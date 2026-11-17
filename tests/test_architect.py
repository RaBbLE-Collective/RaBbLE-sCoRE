"""The Architect: an admin account made locally with hidden input, who alone can mint and revoke invites."""

import sys
import time

import pytest
from fastapi.testclient import TestClient

import accounts
import db
import mailer
import rate_limit

sys.path.insert(0, "server/tools")
import make_architect  # noqa: E402

PW = "a long architect passphrase"


@pytest.fixture()
def env(data_dir, monkeypatch):
    for k in ("ACCOUNT_CAP", "REQUIRE_VERIFIED_EMAIL", "AUTH_IP_HEADER", "MAIL_PROVIDER", "SIGNUP_INVITE_ONLY", "RABBLE_ADMIN_KEY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("DEMO_MODE", "false")
    rate_limit._windows.clear()
    mailer.set_mailer(mailer.ConsoleMailer())
    import main
    yield TestClient(main.app)
    mailer.set_mailer(None)
    rate_limit._windows.clear()


def run_tool(monkeypatch, argv, answers, passwords):
    """Drive make_architect as a terminal user would: typed answers and hidden passwords, no real tty."""
    monkeypatch.setattr(sys, "argv", ["make_architect"] + argv)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    a, p = iter(answers), iter(passwords)
    monkeypatch.setattr("builtins.input", lambda *_: next(a))
    monkeypatch.setattr(make_architect.getpass, "getpass", lambda *_: next(p))
    make_architect.main()


def login(c, ident, pw):
    return c.post("/api/v1/auth/login", json={"identifier": ident, "password": pw})


def bearer(c, ident, pw):
    return {"Authorization": f"Bearer {login(c, ident, pw).json()['token']}"}


def make(monkeypatch, username="architect", email="arch@example.com", pw=PW):
    run_tool(monkeypatch, [], [username, email], [pw, pw])


def test_tool_refuses_without_a_terminal(data_dir, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["make_architect"])
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    with pytest.raises(SystemExit) as e:
        make_architect.main()
    assert "terminal" in str(e.value)
    assert db.query_one("SELECT COUNT(*) AS n FROM accounts")["n"] == 0


def test_tool_makes_a_verified_admin_who_can_sign_in(env, monkeypatch, capsys):
    make(monkeypatch)
    out = capsys.readouterr().out
    assert PW not in out
    r = login(env, "architect", PW)
    assert r.status_code == 200 and r.json()["tier"] == "admin" and r.json()["email_verified"] is True
    assert login(env, "arch@example.com", PW).status_code == 200          # email works too


def test_tool_rejects_short_password_and_mismatch(data_dir, monkeypatch):
    with pytest.raises(SystemExit):
        run_tool(monkeypatch, [], ["architect", "a@example.com"], ["short", "short"])
    with pytest.raises(SystemExit):
        run_tool(monkeypatch, [], ["architect", "a@example.com"], [PW, PW + "x"])
    assert db.query_one("SELECT COUNT(*) AS n FROM accounts")["n"] == 0


def test_only_the_architect_can_mint_list_and_revoke_invites(env, monkeypatch):
    make(monkeypatch)
    h = bearer(env, "architect", PW)
    made = env.post("/api/v1/admin/invites", json={"note": "mark personal"}, headers=h)
    assert made.status_code == 201
    tok = made.json()["token"]
    listed = env.get("/api/v1/admin/invites", headers=h).json()["invites"]
    assert [(i["state"], i["note"]) for i in listed] == [("live", "mark personal")] and listed[0]["token"] == tok
    assert env.delete(f"/api/v1/admin/invites/{tok}", headers=h).status_code == 200
    assert env.delete(f"/api/v1/admin/invites/{tok}", headers=h).status_code == 404


def test_an_ordinary_member_cannot_touch_admin(env, monkeypatch):
    import users
    tok = users.create_invite(created_by="t").token
    assert env.post("/api/v1/auth/signup", json={"username": "member1", "email": "m@example.com",
                                                 "password": "correct horse battery", "invite_token": tok}).status_code == 201
    h = bearer(env, "member1", "correct horse battery")
    assert env.post("/api/v1/admin/invites", json={}, headers=h).status_code == 403
    assert env.get("/api/v1/admin/invites", headers=h).status_code == 403
    assert env.get("/api/v1/admin/export", headers=h).status_code == 403
    assert env.get("/api/v1/admin/invites").status_code in (401, 403)         # no credentials at all


def test_no_admin_key_means_no_key_path(env, monkeypatch):
    assert env.post("/api/v1/admin/invites", json={}, headers={"X-Admin-Key": ""}).status_code in (401, 403)
    assert env.post("/api/v1/admin/invites", json={}, headers={"X-Admin-Key": "guess"}).status_code in (401, 403)


def test_signup_cannot_take_the_architect_name_and_admins_leave_the_cap(env, monkeypatch):
    r = env.post("/api/v1/auth/signup", json={"username": "architect", "email": "x@example.com", "password": "correct horse battery"})
    assert r.status_code == 422
    make(monkeypatch)
    assert accounts.count_accounts() == 0                                      # the Architect does not use a slot


def test_promote_existing_account(env, monkeypatch):
    import users
    tok = users.create_invite(created_by="t").token
    env.post("/api/v1/auth/signup", json={"username": "markp", "email": "mp@example.com",
                                          "password": "correct horse battery", "invite_token": tok})
    old = bearer(env, "markp", "correct horse battery")
    time.sleep(0.01)
    run_tool(monkeypatch, ["--promote"], ["markp"], [])
    assert env.get("/api/v1/admin/invites", headers=old).status_code == 401    # old token ended with the tier change
    assert env.get("/api/v1/admin/invites", headers=bearer(env, "markp", "correct horse battery")).status_code == 200


def test_reset_password_ends_sessions_and_is_admin_only(env, monkeypatch):
    make(monkeypatch)
    old = bearer(env, "architect", PW)
    time.sleep(0.01)
    new = "another long architect passphrase"
    run_tool(monkeypatch, ["--reset-password"], ["architect"], [new, new])
    assert env.get("/api/v1/admin/invites", headers=old).status_code == 401
    assert login(env, "architect", PW).status_code == 401
    assert login(env, "architect", new).status_code == 200
    import users
    tok = users.create_invite(created_by="t").token
    env.post("/api/v1/auth/signup", json={"username": "plain1", "email": "p@example.com",
                                          "password": "correct horse battery", "invite_token": tok})
    with pytest.raises(SystemExit):
        run_tool(monkeypatch, ["--reset-password"], ["plain1"], ["x" * 20, "x" * 20])


def test_invite_lifetime_is_bounded(env, monkeypatch):
    make(monkeypatch)
    h = bearer(env, "architect", PW)
    assert env.post("/api/v1/admin/invites", json={"expires_days": 0}, headers=h).status_code == 422
    assert env.post("/api/v1/admin/invites", json={"expires_days": 3650}, headers=h).status_code == 422
    assert env.post("/api/v1/admin/invites", json={"expires_days": 30, "note": "x" * 121}, headers=h).status_code == 422
    assert env.post("/api/v1/admin/invites", json={"expires_days": 30}, headers=h).status_code == 201


# ── reserved names, reset exemption, non-human accounts ────────────────────────

import make_entity  # noqa: E402


@pytest.mark.parametrize("name", ["RaBbLE-Architect", "architect1", "rabble_admin", "myadmin", "Rabble", "the-rabble"])
def test_brand_and_office_names_are_reserved_at_signup(env, name):
    r = env.post("/api/v1/auth/signup", json={"username": name, "email": "x@example.com", "password": "correct horse battery"})
    assert r.status_code == 422 and "reserved" in r.json()["detail"]


def test_the_tool_defaults_to_the_architect_office_name(env, monkeypatch):
    run_tool(monkeypatch, [], ["", "arch@example.com"], [PW, PW])      # blank name takes the default
    assert accounts.by_username("rabble-architect")["username"] == "RaBbLE-Architect"
    assert login(env, "rabblE-architect", PW).json()["tier"] == "admin"


def test_email_reset_never_reaches_the_architect(env, monkeypatch):
    box = mailer.ConsoleMailer()
    mailer.set_mailer(box)
    make(monkeypatch)
    r = env.post("/api/v1/auth/forgot-password", json={"email": "arch@example.com"})
    assert r.status_code == 200 and not box.outbox                      # same reply as any account, no mail
    uid = accounts.by_username("architect")["user_id"]
    raw = accounts.issue_token(uid, "reset")                            # even a minted token is refused
    bad = env.post("/api/v1/auth/reset-password", json={"token": raw, "new_password": "brand new long passphrase"})
    assert bad.status_code == 400
    assert login(env, "architect", PW).status_code == 200               # password unchanged


def run_entity(monkeypatch, argv, answers):
    monkeypatch.setattr(sys, "argv", ["make_entity"] + argv)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    a = iter(answers)
    monkeypatch.setattr("builtins.input", lambda *_: next(a))
    make_entity.main()


def entity_key(out: str) -> str:
    import re
    return re.search(r"(rbbl_[\w-]+)", out).group(1)


def test_entity_tool_refuses_a_pipe(data_dir, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["make_entity"])
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    with pytest.raises(SystemExit):
        make_entity.main()
    assert db.query_one("SELECT COUNT(*) AS n FROM accounts")["n"] == 0


def test_entity_account_uses_a_key_and_cannot_sign_in(env, monkeypatch, capsys):
    run_entity(monkeypatch, [], [""])                                   # default name RaBbLE
    key = entity_key(capsys.readouterr().out)
    acct = accounts.by_username("rabble")
    assert acct["kind"] == "entity" and acct["email_verified"] == 1
    # the key authenticates as a member with the entity tier
    me = env.get("/api/v1/auth/me", headers={"X-API-Key": key})
    assert me.status_code == 200 and me.json()["tier"] == "entity" and me.json()["name"] == "RaBbLE"
    # but nothing else: no password sign-in (by name or its placeholder email), no admin, no reset
    assert login(env, "RaBbLE", "anything at all here").status_code == 401
    assert login(env, "rabble@entity.invalid", key).status_code == 401
    assert env.post("/api/v1/admin/invites", json={}, headers={"X-API-Key": key}).status_code == 403
    box = mailer.ConsoleMailer(); mailer.set_mailer(box)
    env.post("/api/v1/auth/forgot-password", json={"email": "rabble@entity.invalid"})
    assert not box.outbox
    # only the hash of the key is stored; the entity does not use a human slot under the cap
    assert key not in str(db.query("SELECT * FROM users")) and accounts.count_accounts() == 0


def test_entity_key_rotation_ends_the_old_key(env, monkeypatch, capsys):
    run_entity(monkeypatch, [], ["RaBbLE"])
    old = entity_key(capsys.readouterr().out)
    run_entity(monkeypatch, ["--rotate-key"], ["RaBbLE"])
    new = entity_key(capsys.readouterr().out)
    assert old != new
    assert env.get("/api/v1/auth/me", headers={"X-API-Key": old}).status_code == 401
    assert env.get("/api/v1/auth/me", headers={"X-API-Key": new}).status_code == 200


def test_entity_rotation_refuses_a_human(env, monkeypatch):
    import users
    tok = users.create_invite(created_by="t").token
    env.post("/api/v1/auth/signup", json={"username": "person1", "email": "p1@example.com",
                                          "password": "correct horse battery", "invite_token": tok})
    with pytest.raises(SystemExit):
        run_entity(monkeypatch, ["--rotate-key"], ["person1"])


# ── revoking guests ────────────────────────────────────────────────────────────

import revoke_guests  # noqa: E402


def _guest(n):
    import users
    key = f"rbbl_guestkey_{n}"
    users.create_user(users.UserProfile(id=f"g{n}", handle=f"guest-{n}", display_name="Guest", join_date="2026-01-01",
                                        intention="", api_key=key, tier="guest"))
    db.execute("INSERT INTO guest_turns_by_user (day, user_id, n) VALUES ('2026-11-17', ?, 3)", (f"g{n}",))
    return key


def run_revoke(monkeypatch, argv, confirm):
    monkeypatch.setattr(sys, "argv", ["revoke_guests"] + argv)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda *_: confirm)
    revoke_guests.main()


def test_revoke_guests_ends_their_keys_and_spares_everyone_else(env, monkeypatch, capsys):
    k1, k2 = _guest(1), _guest(2)
    make(monkeypatch)                                                      # the Architect is not a guest
    assert env.get("/api/v1/auth/me", headers={"X-API-Key": k1}).status_code == 200
    run_revoke(monkeypatch, [], "REVOKE")
    assert "Revoked 2 guest(s); 0 left." in capsys.readouterr().out
    assert env.get("/api/v1/auth/me", headers={"X-API-Key": k1}).status_code == 401
    assert env.get("/api/v1/auth/me", headers={"X-API-Key": k2}).status_code == 401
    assert db.query_one("SELECT COUNT(*) AS n FROM guest_turns_by_user")["n"] == 0
    assert login(env, "architect", PW).status_code == 200


def test_revoke_guests_needs_the_word_and_a_terminal(env, monkeypatch):
    _guest(1)
    with pytest.raises(SystemExit):
        run_revoke(monkeypatch, [], "yes")
    assert db.query_one("SELECT COUNT(*) AS n FROM users WHERE tier = 'guest'")["n"] == 1
    run_revoke(monkeypatch, ["--dry-run"], "REVOKE")
    assert db.query_one("SELECT COUNT(*) AS n FROM users WHERE tier = 'guest'")["n"] == 1
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    with pytest.raises(SystemExit):
        revoke_guests.main()
