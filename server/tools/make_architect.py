"""
Create (or promote, or reset) the Architect: the one admin account that can mint invites and use /admin.

Run it yourself, in a real terminal, on a machine that has the database in its environment:
    cd server && ../.venv/bin/python tools/make_architect.py
It asks for the username, email and password with hidden input, and never prints or logs the password.
It refuses to run without a terminal, so nothing can pipe a password into it. The account is created
verified (no mail needed) with tier "admin"; signup can never produce that tier.

  --promote         the account (by username or email) already exists: make it admin, keep its password
  --reset-password  the break-glass: set a new password for an existing admin and end all its sessions.
                    Whoever can run this against the database is the recovery path, so keep that access private.

Database: the same env as the server (DATABASE_URL for Postgres, else DATA_DIR/rabble.db for SQLite).
"""

import argparse
import getpass
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import accounts   # noqa: E402
import db         # noqa: E402

ADMIN_PW_MIN = 14
_USER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{2,23}$")


def _ask_password(confirm: bool) -> str:
    pw = getpass.getpass("Password (hidden): ")
    if confirm and getpass.getpass("Again: ") != pw:
        sys.exit("The two passwords differ.")
    if len(pw) < ADMIN_PW_MIN:
        sys.exit(f"Use at least {ADMIN_PW_MIN} characters for the Architect.")
    return pw


def _make_admin(user_id: str) -> None:
    db.execute("UPDATE users SET tier = 'admin', updated_at = ? WHERE id = ?", (db.now_ts(), user_id))
    accounts.mark_verified(user_id)
    accounts.revoke_all(user_id)     # any token minted before the tier change must not outlive it


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--promote", action="store_true")
    ap.add_argument("--reset-password", action="store_true")
    args = ap.parse_args()
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        sys.exit("Run this in a terminal yourself: it asks for the password and will not take it from a pipe.")
    db.conn()

    if args.reset_password or args.promote:
        who = input("Existing username or email: ").strip()
        acct = accounts.by_identifier(who)
        if not acct:
            sys.exit("No such account.")
        if args.promote:
            _make_admin(acct["user_id"])
            print(f"{acct['username']} is now an admin.")
            return
        row = db.query_one("SELECT tier FROM users WHERE id = ?", (acct["user_id"],))
        if not row or row["tier"] != "admin":
            sys.exit("That account is not an admin; --reset-password is only for the Architect.")
        accounts.set_password(acct["user_id"], _ask_password(confirm=True))
        accounts.revoke_all(acct["user_id"])
        print(f"Password changed for {acct['username']}; every session was ended.")
        return

    username = input("Architect username [architect]: ").strip() or "architect"
    if not _USER.match(username):
        sys.exit("Username: 3-24 characters, letters, digits, '-' or '_'.")
    email = input("Architect email (the RaBbLE address you control): ").strip().lower()
    if "@" not in email:
        sys.exit("That does not look like an email address.")
    pw = _ask_password(confirm=True)
    try:
        acct = accounts.create_account(username, email, pw)
    except ValueError as dup:
        sys.exit(f"That {dup} is already taken. Use --promote to make an existing account the Architect.")
    _make_admin(acct["user_id"])
    print(f"Architect '{username}' created: verified, admin. Sign in with the username or the email.")


if __name__ == "__main__":
    main()
