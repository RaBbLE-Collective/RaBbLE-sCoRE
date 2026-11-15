"""
The database: SQLite (stdlib) at DATA_DIR/score.db, the stopgap until Postgres (Neon).

Plain portable SQL only, `?` placeholders, numbered files in migrations/ applied in
order by the tiny runner below. Moving to Postgres means swapping connect() and the
placeholder; the schema files stay.

One connection, one lock: sCoRE is small and single-process. Writes mark the db dirty
so snapshot.py knows to upload a fresh copy to R2 (the host's disk is ephemeral);
bookkeeping writes pass track=False so counting a snapshot never causes another.
"""

import logging
import os
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

logger = logging.getLogger("rabble.db")

MIGRATIONS = Path(__file__).parent / "migrations"

_lock = threading.RLock()
_conn: sqlite3.Connection | None = None
_path: Path | None = None
_hooks: list[Callable[[], None]] = []
dirty_since: float | None = None   # monotonic time of the first unsnapshotted write


def now_ts() -> str:
    """Row timestamp: UTC, fixed width (microseconds always present), so string order is time order."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def db_path() -> Path:
    return Path(os.getenv("DATA_DIR", "data")) / "score.db"


def register_init(fn: Callable[[], None]) -> None:
    """Run fn after the schema is current on every fresh open (users.py imports its old JSON files)."""
    if fn not in _hooks:
        _hooks.append(fn)


def _statements(sql: str):
    """Split a migration file into statements (the runner needs them one by one to stay in one transaction)."""
    buf = ""
    for line in sql.splitlines(keepends=True):
        buf += line
        if sqlite3.complete_statement(buf):
            if buf.strip():
                yield buf
            buf = ""
    if buf.strip() and not all(l.strip().startswith("--") or not l.strip() for l in buf.splitlines()):
        yield buf


def _migrate(conn: sqlite3.Connection) -> None:
    """Each file runs in one transaction with its version row: a crash or a bad statement
    leaves nothing half-applied (DDL is transactional in SQLite and Postgres)."""
    conn.isolation_level = None   # we issue BEGIN/COMMIT ourselves
    conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations (version TEXT PRIMARY KEY, applied_at TEXT NOT NULL)")
    done = {r[0] for r in conn.execute("SELECT version FROM schema_migrations")}
    for f in sorted(MIGRATIONS.glob("*.sql")):
        if f.stem in done:
            continue
        conn.execute("BEGIN")
        try:
            for stmt in _statements(f.read_text()):
                conn.execute(stmt)
            conn.execute("INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
                         (f.stem, datetime.now(timezone.utc).isoformat()))
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        logger.info("migration applied: %s", f.stem)
    conn.isolation_level = ""


def conn() -> sqlite3.Connection:
    global _conn, _path
    with _lock:
        path = db_path()
        if _conn is not None and _path == path:
            return _conn
        close()
        path.parent.mkdir(parents=True, exist_ok=True)
        c = sqlite3.connect(path, check_same_thread=False)
        c.row_factory = sqlite3.Row
        _migrate(c)
        _conn, _path = c, path
        for hook in _hooks:
            hook()
        return c


def close() -> None:
    global _conn, _path
    with _lock:
        if _conn is not None:
            _conn.close()
        _conn = _path = None


def execute(sql: str, params: tuple = (), *, track: bool = True) -> int:
    """One write, committed. Returns the affected row count."""
    global dirty_since
    with _lock:
        cur = conn().execute(sql, params)
        conn().commit()
        if track and dirty_since is None:
            dirty_since = time.monotonic()
        return cur.rowcount


def query(sql: str, params: tuple = ()) -> list[dict]:
    with _lock:
        return [dict(r) for r in conn().execute(sql, params).fetchall()]


def query_one(sql: str, params: tuple = ()) -> dict | None:
    rows = query(sql, params)
    return rows[0] if rows else None


def meta_get(key: str) -> str | None:
    row = query_one("SELECT value FROM meta WHERE key = ?", (key,))
    return row["value"] if row else None


def meta_set(key: str, value: str, *, track: bool = False) -> None:
    execute("INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value), track=track)


def mark_dirty() -> None:
    global dirty_since
    if dirty_since is None:
        dirty_since = time.monotonic()


def clear_dirty() -> None:
    global dirty_since
    dirty_since = None


def backup(dest: Path) -> None:
    """A consistent copy of the live db (sqlite3 backup API), safe while writes continue."""
    with _lock:
        out = sqlite3.connect(dest)
        try:
            conn().backup(out)
        finally:
            out.close()


def replace_with(snapshot: bytes, merge_local: bool) -> None:
    """Swap in a restored snapshot. With merge_local, rows written locally since (users,
    invites, guest counters and caps, R2 counters, missing meta) are folded into it first, so a late restore loses nothing."""
    with _lock:
        path = db_path()
        tmp = path.with_suffix(".restore")
        tmp.write_bytes(snapshot)
        close()
        if merge_local and path.exists():
            lc = sqlite3.connect(path)             # a local db from before a migration gets it first
            try:
                _migrate(lc)
            finally:
                lc.close()
            c = sqlite3.connect(tmp)
            try:
                _migrate(c)
                c.execute("ATTACH ? AS loc", (str(path),))
                # Rows on both sides: the newer updated_at wins; rows only local are added.
                for table, key in (("users", "id"), ("invites", "token")):
                    cols = [r[1] for r in c.execute(f"PRAGMA main.table_info({table})")]
                    lcols = {r[1] for r in c.execute(f"PRAGMA loc.table_info({table})")}
                    use = ", ".join(x for x in cols if x in lcols)
                    c.execute(f"INSERT INTO main.{table} ({use}) SELECT {use} FROM loc.{table} "
                              f"WHERE {key} NOT IN (SELECT {key} FROM main.{table})")
                    # consumed state (invites) only moves forward: it is excluded from the newer-wins copy
                    sets = ", ".join(f"{x} = l.{x}" for x in cols
                                     if x in lcols and x != key and not (table == "invites" and x in ("consumed", "consumed_by")))
                    c.execute(f"UPDATE main.{table} AS m SET {sets} FROM loc.{table} AS l "
                              f"WHERE m.{key} = l.{key} AND l.updated_at > m.updated_at")
                c.execute("UPDATE main.invites AS m SET consumed = 1, consumed_by = COALESCE(m.consumed_by, l.consumed_by), "
                          "updated_at = MAX(m.updated_at, l.updated_at) FROM loc.invites AS l "
                          "WHERE m.token = l.token AND l.consumed = 1 AND m.consumed = 0")
                c.execute("UPDATE main.invites AS m SET consumed_by = l.consumed_by FROM loc.invites AS l "
                          "WHERE m.token = l.token AND m.consumed = 1 AND m.consumed_by IS NULL")
                c.execute("INSERT INTO main.guest_turns (day, n) SELECT day, n FROM loc.guest_turns WHERE true "
                          "ON CONFLICT(day) DO UPDATE SET n = MAX(n, excluded.n)")
                # per-guest daily cap and hourly issuance limit must not reset on a late restore
                c.execute("INSERT INTO main.guest_turns_by_user (day, user_id, n) SELECT day, user_id, n FROM loc.guest_turns_by_user "
                          "WHERE true ON CONFLICT(day, user_id) DO UPDATE SET n = MAX(n, excluded.n)")
                c.execute("INSERT INTO main.guest_issue (hour, n) SELECT hour, n FROM loc.guest_issue "
                          "WHERE true ON CONFLICT(hour) DO UPDATE SET n = MAX(n, excluded.n)")
                c.execute("INSERT OR IGNORE INTO main.meta (key, value) SELECT key, value FROM loc.meta")   # restored values win
                c.execute("INSERT INTO main.r2_usage (month, class_a, class_b) SELECT month, class_a, class_b FROM loc.r2_usage "
                          "WHERE true ON CONFLICT(month) DO UPDATE SET class_a = MAX(class_a, excluded.class_a), "
                          "class_b = MAX(class_b, excluded.class_b)")
                c.commit()
                c.execute("DETACH loc")
            finally:
                c.close()
        tmp.replace(path)
