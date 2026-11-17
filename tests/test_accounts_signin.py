"""AC2 sign-in: signup/login, verification, reset, refresh rotation, cookies. SQLite, offline."""

import re
import time

import pytest
from fastapi.testclient import TestClient

import accounts
import db
import mailer
import rate_limit

PW = "correct horse battery"


@pytest.fixture()
def env(data_dir, monkeypatch):
    for k in ("ACCOUNT_CAP", "REQUIRE_VERIFIED_EMAIL", "AUTH_IP_HEADER", "MAIL_PROVIDER"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("DEMO_MODE", "false")
    rate_limit._windows.clear()
    box = mailer.ConsoleMailer()
    mailer.set_mailer(box)
    import main
    yield TestClient(main.app), box
    mailer.set_mailer(None)
    rate_limit._windows.clear()


def signup(c, username="mark", email="mark@example.com", password=PW):
    return c.post("/api/v1/auth/signup", json={"username": username, "email": email, "password": password})


def token_from(box, idx=-1):
    return re.search(r"token=([\w-]+)", box.outbox[idx]["text"]).group(1)


def verify(c, box):
    assert c.post("/api/v1/auth/verify-email", json={"token": token_from(box)}).status_code == 200


def login(c, ident="mark", password=PW):
    return c.post("/api/v1/auth/login", json={"identifier": ident, "password": password})


def cookie_hdr(r):
    return {"Cookie": f"rabble_refresh={r.cookies['rabble_refresh']}"} if "rabble_refresh" in r.cookies else {}


def rt(r):
    m = re.search(r"rabble_refresh=([^;]+)", r.headers["set-cookie"])
    return {"Cookie": f"rabble_refresh={m.group(1)}"}


def ready(c, box, **kw):
    assert signup(c, **kw).status_code == 201
    verify(c, box)


def test_signup_login_happy_path(env):
    c, box = env
    r = signup(c)
    assert r.status_code == 201 and r.json()["email_verified"] is False
    assert box.outbox[0]["to"] == "mark@example.com" and "/verify-email?token=" in box.outbox[0]["text"]
    verify(c, box)
    r = login(c, "MARK")                      # username is case-insensitive
    assert r.status_code == 200
    jwt = r.json()["token"]
    me = c.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {jwt}"})
    assert me.status_code == 200 and me.json()["name"] == "mark"
    assert login(c, "Mark@Example.COM").status_code == 200      # or by email
    acct = accounts.by_username("mark")
    assert acct["password_hash"].startswith("$argon2id$") and PW not in acct["password_hash"]


def test_unverified_login_refused_unless_switched_off(env, monkeypatch):
    c, box = env
    signup(c)
    r = login(c)
    assert r.status_code == 403 and r.json()["code"] == "email_not_verified"
    monkeypatch.setenv("REQUIRE_VERIFIED_EMAIL", "false")
    assert login(c).status_code == 200


def test_duplicates(env):
    c, box = env
    assert signup(c).status_code == 201
    assert signup(c, username="MARK", email="other@example.com").status_code == 409
    # a taken email gets the same 201 as a new one (M6); nothing is created, the owner is told by mail
    r = signup(c, username="other", email="MARK@example.com")
    assert r.status_code == 201 and r.json()["verification_sent"] is True and "user_id" not in r.json()
    assert db.query_one("SELECT COUNT(*) AS n FROM accounts")["n"] == 1
    assert box.outbox[-1]["to"] == "mark@example.com" and "already has an account" in box.outbox[-1]["text"]
    assert "token=" not in box.outbox[-1]["text"]


def test_signup_reply_same_for_new_and_taken_email(env):
    c, box = env
    new = signup(c, username="one", email="one@example.com")
    taken = signup(c, username="two", email="ONE@example.com")
    assert new.status_code == taken.status_code == 201
    assert new.json() == {**taken.json(), "username": "one"}


def test_account_cap_waitlist(env, monkeypatch):
    c, box = env
    monkeypatch.setenv("ACCOUNT_CAP", "1")
    ready(c, box)                                      # only verified accounts hold a slot
    r = signup(c, username="second", email="s@example.com")
    assert r.status_code == 403 and r.json()["waitlist"] is True and "rooms are full" in r.json()["detail"]
    monkeypatch.setenv("ACCOUNT_CAP", "2")
    assert signup(c, username="second", email="s@example.com").status_code == 201


def test_cap_ignores_guests(env, monkeypatch):
    c, box = env
    import users
    users.create_user(users.UserProfile(id="g1", handle="guest-1", display_name="g", join_date="2026-01-01",
                                        intention="", api_key="k", tier="guest"))
    monkeypatch.setenv("ACCOUNT_CAP", "1")
    assert signup(c).status_code == 201


@pytest.mark.parametrize("pw", ["short", "x" * 129, "mark", "mark@example.com", "aaaaaaaaaaaa"])
def test_password_rules(env, pw):
    c, box = env
    assert signup(c, username="mark", email="mark@example.com", password=pw).status_code == 422
    assert signup(c, username="markmark", email="markmark@example.com", password="markmark").status_code == 422


def test_username_rules(env):
    c, box = env
    for bad in ("ab", "has space", "-lead", "admin", "x" * 25):
        assert signup(c, username=bad).status_code == 422


def test_wrong_password_and_unknown_user_same_error(env, monkeypatch):
    c, box = env
    ready(c, box)
    calls = []
    real = accounts._ph

    class Spy:
        def verify(self, h, p):
            calls.append(1)
            return real.verify(h, p)

        def __getattr__(self, k):
            return getattr(real, k)

    monkeypatch.setattr(accounts, "_ph", Spy())
    a = login(c, "mark", "wrong password 123")
    b = login(c, "nobody", "wrong password 123")
    assert a.status_code == b.status_code == 401 and a.json() == b.json()
    assert len(calls) == 2          # the unknown user paid for an Argon2 verify too


def test_login_rate_limit_per_account(env):
    c, box = env
    ready(c, box)
    codes = [login(c, "mark", "bad bad bad bad").status_code for _ in range(11)]
    assert codes[:10] == [401] * 10 and codes[10] == 429
    assert login(c, "mark").status_code == 429     # the right password does not bypass it


def test_signup_rate_limit_per_ip(env):
    c, box = env
    codes = [signup(c, username=f"user{i}", email=f"u{i}@example.com").status_code for i in range(6)]
    assert codes[:5] == [201] * 5 and codes[5] == 429


def test_forgot_is_enumeration_safe(env):
    c, box = env
    ready(c, box)
    n = len(box.outbox)
    a = c.post("/api/v1/auth/forgot-password", json={"email": "mark@example.com"})
    b = c.post("/api/v1/auth/forgot-password", json={"email": "ghost@example.com"})
    assert a.status_code == b.status_code == 200 and a.json() == b.json() and a.headers.get("content-length") == b.headers.get("content-length")
    assert len(box.outbox) == n + 1 and box.outbox[-1]["to"] == "mark@example.com"
    # over the per-account limit the reply still does not change
    for _ in range(4):
        rate_limit._windows.pop("auth:forgot:ip:testclient", None)
        r = c.post("/api/v1/auth/forgot-password", json={"email": "mark@example.com"})
        assert r.json() == a.json()
    assert len(box.outbox) == n + 3
    assert db.query_one("SELECT COUNT(*) AS n FROM auth_tokens WHERE kind = 'reset' AND user_id = ?",
                        (accounts.by_username("mark")["user_id"],))["n"] == 3


def test_tokens_stored_hashed(env):
    c, box = env
    signup(c)
    raw = token_from(box)
    rows = db.query("SELECT token_hash FROM auth_tokens")
    assert rows and all(raw not in r["token_hash"] and len(r["token_hash"]) == 64 for r in rows)


def test_verify_token_single_use(env):
    c, box = env
    signup(c)
    t = token_from(box)
    assert c.post("/api/v1/auth/verify-email", json={"token": t}).status_code == 200
    assert c.post("/api/v1/auth/verify-email", json={"token": t}).status_code == 400
    assert c.post("/api/v1/auth/verify-email", json={"token": "nope"}).status_code == 400


def test_token_expiry(env, monkeypatch):
    c, box = env
    signup(c)
    t = token_from(box)
    db.execute("UPDATE auth_tokens SET expires_at = '2000-01-01T00:00:00.000000Z'")
    assert c.post("/api/v1/auth/verify-email", json={"token": t}).status_code == 400
    assert accounts.by_username("mark")["email_verified"] == 0


def test_resend_verification(env):
    c, box = env
    signup(c)
    old = token_from(box)
    r = c.post("/api/v1/auth/resend-verification", json={"email": "mark@example.com"})
    g = c.post("/api/v1/auth/resend-verification", json={"email": "ghost@example.com"})
    assert r.json() == g.json() and len(box.outbox) == 2
    assert c.post("/api/v1/auth/verify-email", json={"token": old}).status_code == 400   # superseded
    verify(c, box)


def _reset_token(c, box):
    c.post("/api/v1/auth/forgot-password", json={"email": "mark@example.com"})
    return token_from(box)


def test_reset_single_use_and_revokes_sessions(env):
    c, box = env
    ready(c, box)
    l1 = login(c)
    old_jwt, old_cookie = l1.json()["token"], rt(l1)
    time.sleep(0.01)
    t = _reset_token(c, box)
    # a weak new password does not burn the token
    assert c.post("/api/v1/auth/reset-password", json={"token": t, "new_password": "short"}).status_code == 422
    assert c.post("/api/v1/auth/reset-password", json={"token": t, "new_password": "brand new passphrase"}).status_code == 200
    assert c.post("/api/v1/auth/reset-password", json={"token": t, "new_password": "another passphrase!"}).status_code == 400
    # old access JWT and old refresh cookie are dead
    assert c.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {old_jwt}"}).status_code == 401
    assert c.post("/api/v1/auth/refresh", headers=old_cookie).status_code == 401
    assert login(c).status_code == 401
    new = login(c, "mark", "brand new passphrase")
    assert new.status_code == 200
    assert c.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {new.json()['token']}"}).status_code == 200


def test_reset_verifies_email(env):
    c, box = env
    signup(c)
    t = _reset_token(c, box)
    assert c.post("/api/v1/auth/reset-password", json={"token": t, "new_password": "brand new passphrase"}).status_code == 200
    assert login(c, "mark", "brand new passphrase").status_code == 200


def test_refresh_rotation(env):
    c, box = env
    ready(c, box)
    l = login(c)
    c1 = rt(l)
    r = c.post("/api/v1/auth/refresh", headers=c1)
    assert r.status_code == 200 and r.json()["token"]
    c2 = rt(r)
    assert c2 != c1
    assert c.post("/api/v1/auth/refresh", headers=c2).status_code == 200
    assert c.post("/api/v1/auth/refresh").status_code == 401        # no cookie


def test_refresh_reuse_revokes_family(env):
    c, box = env
    ready(c, box)
    c1 = rt(login(c))
    r2 = c.post("/api/v1/auth/refresh", headers=c1)
    c2 = rt(r2)
    assert c.post("/api/v1/auth/refresh", headers=c1).status_code == 401      # replay of the used token
    assert c.post("/api/v1/auth/refresh", headers=c2).status_code == 401      # the family is gone
    # a separate login is a separate family and survives
    c3 = rt(login(c))
    assert c.post("/api/v1/auth/refresh", headers=c3).status_code == 200


def test_refresh_expired(env):
    c, box = env
    ready(c, box)
    c1 = rt(login(c))
    db.execute("UPDATE refresh_tokens SET expires_at = '2000-01-01T00:00:00.000000Z'")
    assert c.post("/api/v1/auth/refresh", headers=c1).status_code == 401


def test_refresh_tokens_stored_hashed(env):
    c, box = env
    ready(c, box)
    raw = rt(login(c))["Cookie"].split("=", 1)[1]
    assert all(raw not in r["token_hash"] for r in db.query("SELECT token_hash FROM refresh_tokens"))


def test_logout_revokes_and_clears(env):
    c, box = env
    ready(c, box)
    c1 = rt(login(c))
    r = c.post("/api/v1/auth/logout", headers=c1)
    assert r.status_code == 200 and "Max-Age=0" in r.headers["set-cookie"]
    assert c.post("/api/v1/auth/refresh", headers=c1).status_code == 401
    assert c.post("/api/v1/auth/logout").status_code == 200      # no cookie is fine


def test_cookie_flags(env):
    c, box = env
    ready(c, box)
    sc = login(c).headers["set-cookie"]
    low = sc.lower()
    assert sc.startswith("rabble_refresh=rt_")
    assert "httponly" in low and "secure" in low and "samesite=lax" in low and "path=/api/v1/auth" in low
    assert "max-age=" in low


def test_rehash_on_login(env, monkeypatch):
    c, box = env
    ready(c, box)
    from argon2 import PasswordHasher
    weak = PasswordHasher(time_cost=1, memory_cost=8, parallelism=1).hash(PW)
    db.execute("UPDATE accounts SET password_hash = ? WHERE username_lower = 'mark'", (weak,))
    assert login(c).status_code == 200
    new = accounts.by_username("mark")["password_hash"]
    assert new != weak and "m=65536" in new
    assert login(c).status_code == 200 and accounts.by_username("mark")["password_hash"] == new


def test_legacy_bcrypt_hash_verifies_then_upgrades(env):
    c, box = env
    ready(c, box)
    import bcrypt
    db.execute("UPDATE accounts SET password_hash = ? WHERE username_lower = 'mark'",
               (bcrypt.hashpw(PW.encode(), bcrypt.gensalt(4)).decode(),))
    assert login(c, "mark", "wrong password 123").status_code == 401
    assert login(c).status_code == 200
    assert accounts.by_username("mark")["password_hash"].startswith("$argon2id$")


def test_salts_differ(env):
    assert accounts.hash_pw(PW) != accounts.hash_pw(PW)


def test_existing_auth_paths_unchanged(env):
    c, box = env
    assert c.get("/api/v1/auth/me").status_code == 401
    assert c.get("/api/v1/auth/me", headers={"X-API-Key": "rbbl_nope"}).status_code == 401
    import users
    users.create_user(users.UserProfile(id="u9", handle="old", display_name="Old", join_date="2026-01-01",
                                        intention="", api_key="rbbl_legacykey", tier="collective"))
    assert c.get("/api/v1/auth/me", headers={"X-API-Key": "rbbl_legacykey"}).json()["user_id"] == "u9"
    import auth
    legacy = auth.make_token("u9", "Old")           # no acct claim: no revocation lookup
    assert c.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {legacy}"}).status_code == 200


def test_no_plaintext_in_logs(env, caplog):
    c, box = env
    with caplog.at_level("DEBUG"):
        ready(c, box)
        login(c)
        login(c, "mark", "wrong password 123")
    assert PW not in caplog.text and "wrong password 123" not in caplog.text


def test_mailer_refuses_half_configured(monkeypatch):
    mailer.set_mailer(None)
    monkeypatch.setenv("MAIL_FROM", "x@example.com")
    with pytest.raises(RuntimeError):
        mailer.get_mailer()
    monkeypatch.delenv("MAIL_FROM")
    assert isinstance(mailer.get_mailer(), mailer.NullMailer)      # unset = no mail, never the console (M5)
    monkeypatch.setenv("MAIL_PROVIDER", "console")
    assert isinstance(mailer.get_mailer(), mailer.ConsoleMailer)


def test_null_mailer_logs_no_link(caplog):
    with caplog.at_level("DEBUG"):
        mailer.NullMailer().send("a@example.com", "Reset", "https://x/reset-password?token=SECRET123")
    assert "SECRET123" not in caplog.text and "a@example.com" not in caplog.text


def test_default_deploy_leaks_no_link(env, caplog, capsys):
    """No MAIL_PROVIDER: signup and forgot send nothing anywhere readable."""
    c, _ = env
    mailer.set_mailer(None)
    with caplog.at_level("DEBUG"):
        assert signup(c).status_code == 201
        c.post("/api/v1/auth/forgot-password", json={"email": "mark@example.com"})
    assert "token=" not in caplog.text and "token=" not in capsys.readouterr().out


# ── NiBbLE S345 regressions ────────────────────────────────────────────────────

def test_refresh_race_loser_cannot_leave_a_live_child(env, monkeypatch):
    """M1: a replay that lands after the winner claimed the parent but before the child exists must not
    leave the child alive."""
    c, box = env
    ready(c, box)
    raw = rt(login(c))["Cookie"].split("=", 1)[1]
    real = accounts.issue_refresh

    def issue_then_replay(user_id, family=None):
        if family:                                    # the winner is mid-rotation: the loser arrives now
            assert accounts.rotate_refresh(raw) is None
        return real(user_id, family)

    monkeypatch.setattr(accounts, "issue_refresh", issue_then_replay)
    assert accounts.rotate_refresh(raw) is None
    assert db.query_one("SELECT COUNT(*) AS n FROM refresh_tokens WHERE revoked = 0")["n"] == 0


def test_reuse_kills_the_access_jwt(env):
    """M2: reuse detection also ends the 8 h access token."""
    c, box = env
    ready(c, box)
    l = login(c)
    jwt = l.json()["token"]
    h = {"Authorization": f"Bearer {jwt}"}
    c1 = rt(l)
    c.post("/api/v1/auth/refresh", headers=c1)
    import auth
    time.sleep(0.01)
    assert auth.decode_token(jwt)["sub"]
    c.post("/api/v1/auth/refresh", headers=c1)              # replay
    with pytest.raises(Exception):
        auth.decode_token(jwt)


def test_logout_kills_the_access_jwt(env):
    c, box = env
    ready(c, box)
    l = login(c)
    import auth
    assert auth.decode_token(l.json()["token"])
    time.sleep(0.01)
    c.post("/api/v1/auth/logout", headers=rt(l))
    with pytest.raises(Exception):
        auth.decode_token(l.json()["token"])


def test_session_check_is_cached(env, monkeypatch):
    """M4: repeated checks of one account JWT do not each hit the database."""
    c, box = env
    ready(c, box)
    jwt = login(c).json()["token"]
    import auth
    n = {"q": 0}
    real = accounts.by_user_id
    monkeypatch.setattr(accounts, "by_user_id", lambda u: (n.__setitem__("q", n["q"] + 1), real(u))[1])
    for _ in range(20):
        auth.decode_token(jwt)
    assert n["q"] <= 1


def test_unverified_accounts_do_not_fill_the_cap_and_are_purged(env, monkeypatch):
    c, box = env
    monkeypatch.setenv("ACCOUNT_CAP", "1")
    for i in range(3):
        assert signup(c, username=f"ghost{i}", email=f"g{i}@example.com").status_code == 201
    ready(c, box, username="real", email="real@example.com")     # not turned away by the three unverified
    assert accounts.count_accounts() == 1
    db.execute("UPDATE accounts SET created_at = '2000-01-01T00:00:00.000000Z' WHERE email_verified = 0")
    accounts.purge_expired()
    assert db.query_one("SELECT COUNT(*) AS n FROM accounts")["n"] == 1
    assert db.query_one("SELECT COUNT(*) AS n FROM users WHERE tier <> 'guest'")["n"] == 1


def test_purge_keeps_unverified_when_verification_is_off(env, monkeypatch):
    c, box = env
    monkeypatch.setenv("REQUIRE_VERIFIED_EMAIL", "false")
    assert signup(c).status_code == 201
    db.execute("UPDATE accounts SET created_at = '2000-01-01T00:00:00.000000Z'")
    accounts.purge_expired()
    assert db.query_one("SELECT COUNT(*) AS n FROM accounts")["n"] == 1


def test_login_budget_shared_by_username_and_email(env):
    c, box = env
    ready(c, box)
    codes = [login(c, "mark" if i % 2 else "mark@example.com", "bad bad bad bad").status_code for i in range(11)]
    assert codes[:10] == [401] * 10 and codes[10] == 429


def test_login_limit_is_per_ip(env):
    """Low: an attacker's failures from one address do not lock the owner out from another."""
    c, box = env
    ready(c, box)
    c.headers["x-forwarded-for"] = "9.9.9.9"
    import os
    os.environ["AUTH_IP_HEADER"] = "x-forwarded-for"
    try:
        for _ in range(11):
            login(c, "mark", "bad bad bad bad")
        assert login(c, "mark").status_code == 429
        c.headers["x-forwarded-for"] = "1.2.3.4"
        assert login(c, "mark").status_code == 200
    finally:
        os.environ.pop("AUTH_IP_HEADER", None)


def test_rate_limit_windows_are_pruned():
    rate_limit._windows.clear()
    rate_limit._windows["old"].append(time.monotonic() - 90000)
    rate_limit._windows["empty"]
    rate_limit._last_prune = 0.0
    rate_limit.peek("anything", 60, 1)
    assert "old" not in rate_limit._windows and "empty" not in rate_limit._windows
