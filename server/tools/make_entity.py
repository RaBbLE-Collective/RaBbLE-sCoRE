"""
Create (or re-key) a non-human account: the one RaBbLE the entity signs in to the Collective with.

Run it yourself, in a real terminal, like make_architect.py (it refuses a pipe):
    cd server && ../.venv/bin/python tools/make_entity.py            # create; default name RaBbLE
    cd server && ../.venv/bin/python tools/make_entity.py --rotate-key

A non-human account (kind "entity", tier "entity") has no password, no email and no sign-in page.
It authenticates with an API key, sent as the X-API-Key header. The key is printed ONCE, to this
terminal: copy it straight into your vault or the machine's secret store. Only its hash is kept, so
it cannot be shown again, only replaced (--rotate-key ends the old one at once).
It has ordinary member rights and no admin rights; widen that deliberately, never by default.
"""

import argparse
import re
import secrets
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import accounts   # noqa: E402
import db         # noqa: E402
import users      # noqa: E402

_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{2,23}$")


def _new_key() -> str:
    return "rbbl_" + secrets.token_urlsafe(32)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--rotate-key", action="store_true")
    args = ap.parse_args()
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        sys.exit("Run this in a terminal yourself: it prints the API key once and will not run from a pipe.")
    db.conn()

    if args.rotate_key:
        acct = accounts.by_identifier(input("Entity name: ").strip())
        if not acct or acct["kind"] != "entity":
            sys.exit("No such non-human account.")
        key = _new_key()
        db.execute("UPDATE users SET api_key = ?, updated_at = ? WHERE id = ?",
                   (users.hash_api_key(key), db.now_ts(), acct["user_id"]))
        print(f"New key for {acct['username']} (shown once, the old key no longer works):\n\n  {key}\n")
        return

    name = input("Entity name [RaBbLE]: ").strip() or "RaBbLE"
    if not _NAME.match(name):
        sys.exit("Name: 3-24 characters, letters, digits, '-' or '_'.")
    key = _new_key()
    try:
        acct = accounts.create_account(name, f"{name.lower()}@entity.invalid", None, kind="entity", tier="entity", api_key=key)
    except ValueError as dup:
        sys.exit(f"That {dup} is already taken.")
    accounts.mark_verified(acct["user_id"])
    print(f"Entity account '{name}' created. API key (shown once, copy it now):\n\n  {key}\n\n"
          "Send it as the X-API-Key header. It has member rights only.")


if __name__ == "__main__":
    main()
