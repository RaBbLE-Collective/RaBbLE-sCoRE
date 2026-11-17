"""
Revoke Pocket/device guest accounts: delete every users row with tier "guest" (and its per-guest counters).
A guest's API key is only a hash in the database, so deleting the row ends the key at once.

Run it yourself in a terminal against the database in your environment:
    cd server && set -a && . ../.env.local && set +a && ../.venv/bin/python tools/revoke_guests.py [--dry-run]

It lists what it will delete (handles only), then asks you to type REVOKE. Members, the Architect and the entity
are never touched (they are not tier "guest"). It does NOT turn guest minting off (guests remain possible for
testing: unset GUEST_ISSUE_SECRET to close it). Since S347 the beam refuses guest keys and the shared
BEAM_DEVICE_TOKEN unless BEAM_ALLOW_GUEST_POCKETS=true, so on production these accounts are already useless on a
Pocket; this deletes them and their counters.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        sys.exit("Run this in a terminal yourself: it deletes accounts and asks for confirmation.")
    db.conn()
    guests = db.query("SELECT id, handle FROM users WHERE tier = 'guest' ORDER BY handle")
    if not guests:
        print("No guest accounts.")
        return
    print(f"{len(guests)} guest account(s):")
    for g in guests:
        print(f"  {g['handle']}")
    if args.dry_run:
        print("(dry run: nothing deleted)")
        return
    if input("Type REVOKE to delete them and end their keys: ").strip() != "REVOKE":
        sys.exit("Not confirmed. Nothing deleted.")
    ids = [g["id"] for g in guests]
    for uid in ids:
        db.execute("DELETE FROM guest_turns_by_user WHERE user_id = ?", (uid,))
        db.execute("DELETE FROM users WHERE id = ? AND tier = 'guest'", (uid,))
    left = db.query_one("SELECT COUNT(*) AS n FROM users WHERE tier = 'guest'")["n"]
    print(f"Revoked {len(ids)} guest(s); {left} left.")


if __name__ == "__main__":
    main()
