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
