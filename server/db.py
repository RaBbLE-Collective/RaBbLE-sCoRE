"""
The database: SQLite (stdlib) at DATA_DIR/score.db locally, Postgres (psycopg 3) when
DATABASE_URL is set (hosted: Neon, use the POOLED url).

Plain portable SQL only, `?` placeholders (translated to %s here for Postgres, callers
never see it), numbered files in migrations/ applied in order by the tiny runner below;
the same files run on both engines.

One connection, one lock: sCoRE is small and single-process. Writes mark the db dirty
so snapshot.py knows to upload a fresh copy to R2 (the host's disk is ephemeral; Postgres
is durable, so snapshots are SQLite-only); bookkeeping writes pass track=False so
counting a snapshot never causes another.

DATABASE_SCHEMA (optional, Postgres only): created if missing and put on search_path.
Needs a direct (non-pooled) url; tests use it for a throwaway schema.
"""

import logging
import os
import re
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

logger = logging.getLogger("rabble.db")

MIGRATIONS = Path(__file__).parent / "migrations"

_lock = threading.RLock()
_conn = None   # sqlite3.Connection or psycopg.Connection
_path = None   # sqlite: the file; postgres: the url (a changed env reopens)
_hooks: list[Callable[[], None]] = []
dirty_since: float | None = None   # monotonic time of the first unsnapshotted write


def now_ts() -> str:
    """Row timestamp: UTC, fixed width (microseconds always present), so string order is time order."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def db_path() -> Path:
    return Path(os.getenv("DATA_DIR", "data")) / "score.db"


def is_postgres() -> bool:
    return bool(os.getenv("DATABASE_URL"))


def _translate(sql: str, nparams: int) -> str:
    """`?` to `%s` for psycopg, skipping string literals (E'' with backslashes too), "quoted
    identifiers" and comments; literal `%` doubled. Dollar-quoted bodies are refused (use none)."""
    if not nparams:
        return sql
    if re.search(r"\$\w*\$", sql):
        raise ValueError("dollar-quoted strings are not supported by db._translate")
    out, i, n = [], 0, len(sql)
    while i < n:
        ch = sql[i]
        if ch == "-" and sql.startswith("--", i):
            j = sql.find("\n", i)
            j = n if j < 0 else j
            out.append(sql[i:j].replace("%", "%%"))
            i = j
            continue
        if ch == "/" and sql.startswith("/*", i):
            j = sql.find("*/", i + 2)
            j = n if j < 0 else j + 2
            out.append(sql[i:j].replace("%", "%%"))
            i = j
            continue
        if ch in "'\"":
            esc = ch == "'" and i > 0 and sql[i - 1] in "eE" and (i < 2 or not (sql[i - 2].isalnum() or sql[i - 2] == "_"))
            j = i + 1
            while j < n:
                if esc and sql[j] == "\\":
                    j += 2
                    continue
                if sql[j] == ch:
                    if j + 1 < n and sql[j + 1] == ch:   # doubled quote stays inside
                        j += 2
                        continue
                    break
                j += 1
            out.append(sql[i:j + 1].replace("%", "%%"))
            i = j + 1
            continue
        out.append("%%" if ch == "%" else "%s" if ch == "?" else ch)
        i += 1
    return "".join(out)


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


_LOCK_ID = 7266301          # advisory lock key: one booting instance migrates at a time
_PG_TIMEOUTS = "SET LOCAL lock_timeout = '30s'; SET LOCAL statement_timeout = '120s'"   # a migration never waits forever


def _pg_txn_lock(conn) -> None:
    conn.execute("BEGIN")
    for part in _PG_TIMEOUTS.split(";"):
        conn.execute(part)
    conn.execute(f"SELECT pg_advisory_xact_lock({_LOCK_ID})")


def _migrate(conn) -> None:
    """Each file runs in one transaction with its version row: a crash or a bad statement
    leaves nothing half-applied (DDL is transactional in SQLite and Postgres). On Postgres the
    connection is autocommit and we issue BEGIN/COMMIT ourselves, always under the advisory lock."""
    pg = not isinstance(conn, sqlite3.Connection)
    if pg:
        ph = "%s"
        _pg_txn_lock(conn)      # before the first CREATE: two fresh instances must not race on it
    else:
        conn.isolation_level = None
        ph = "?"
        conn.execute("BEGIN")
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations (version TEXT PRIMARY KEY, applied_at TEXT NOT NULL)")
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    done = {(r["version"] if pg else r[0]) for r in conn.execute("SELECT version FROM schema_migrations").fetchall()}
    for f in sorted(MIGRATIONS.glob("*.sql")):
        if f.stem in done:
            continue
        if pg:    # a second booting instance waits here, then sees the file done
            _pg_txn_lock(conn)
            if conn.execute("SELECT 1 FROM schema_migrations WHERE version = %s", (f.stem,)).fetchone():
                conn.execute("COMMIT")
                continue
        else:
            conn.execute("BEGIN")
        try:
            for stmt in _statements(f.read_text()):
                conn.execute(stmt)
            conn.execute(f"INSERT INTO schema_migrations (version, applied_at) VALUES ({ph}, {ph})",
                         (f.stem, datetime.now(timezone.utc).isoformat()))
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        logger.info("migration applied: %s", f.stem)
    if not pg:
        conn.isolation_level = ""


_CONNECT_TIMEOUT = 10       # seconds. A cold Neon compute wakes in about a second or two; this is the ceiling.
_TCP_USER_TIMEOUT_MS = 30000
_PING_AFTER = 30.0         # idle seconds before a write is preceded by a cheap SELECT 1 (see _live)


def _open_pg(url: str):
    """Autocommit connection: every statement is its own transaction, so a read never leaves one
    open (no locks held, nothing pinned under a pgbouncer transaction pooler). Errors are
    re-raised without the driver's text, which can echo the connection string and its password."""
    import psycopg
    from psycopg.rows import dict_row
    schema = os.getenv("DATABASE_SCHEMA")
    if schema:
        if not schema.replace("_", "").isalnum():
            raise ValueError("DATABASE_SCHEMA must be alphanumeric/underscore")
        if "-pooler" in url:
            raise ValueError("DATABASE_SCHEMA needs a direct (non-pooled) DATABASE_URL: search_path is session state")
    failed = None
    try:
        # prepare_threshold=None: a pooled url cannot hold server-side prepared statements.
        # keepalives + tcp_user_timeout: a dead network path errors out in about 30s instead of
        # blocking every thread on _lock forever. (A reachable but frozen server is not covered.)
        c = psycopg.connect(url, row_factory=dict_row, prepare_threshold=None,
                            connect_timeout=_CONNECT_TIMEOUT, autocommit=True,
                            keepalives=1, keepalives_idle=15, keepalives_interval=5, keepalives_count=3,
                            tcp_user_timeout=_TCP_USER_TIMEOUT_MS)
    except Exception as exc:
        failed = type(exc).__name__
    if failed:
        # raised outside the except block: no __context__ chain, so nothing can walk back to the URL
        raise RuntimeError(f"could not connect to Postgres ({failed}); check DATABASE_URL")
    if schema:
        _pg_txn_lock(c)
        c.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')
        c.execute("COMMIT")
        c.execute(f'SET search_path TO "{schema}"')
    return c


def conn():
    global _conn, _path
    with _lock:
        url = os.getenv("DATABASE_URL")
        path = url or db_path()
        if _conn is not None and _path == path:
            return _conn
        close()
        if url:
            c = _open_pg(url)
        else:
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
            try:
                _conn.close()
            except Exception:
                pass
        _conn = _path = None


_last_used = 0.0
_RETRYABLE_STATES = ("08", "57P01", "57P02", "57P03")   # connection-level only; never cancel/lock/deadlock/serialization


def _live():
    """The open Postgres connection. A connection idle for a while (Neon suspends, poolers drop
    idle clients) is probed with SELECT 1 first, so a stale socket fails there and not on a write."""
    import psycopg
    global _last_used
    c = conn()
    if c.closed or c.broken:
        close()
        c = conn()
    elif time.monotonic() - _last_used > _PING_AFTER:
        try:
            c.execute("SELECT 1")
        except psycopg.OperationalError:
            close()
            c = conn()
    _last_used = time.monotonic()
    return c


def _connection_level(exc) -> bool:
    state = getattr(exc, "sqlstate", None)
    return state is None or state.startswith(_RETRYABLE_STATES)


def _run(sql: str, params: tuple, write: bool):
    """Run one statement; returns (rows or None, rowcount). Postgres is autocommit, so each
    statement commits itself and nothing is left open. A READ is retried once on a connection
    error; a WRITE never is (it may already have been applied), the error surfaces and the next
    call reopens the connection."""
    if not is_postgres():
        cur = conn().execute(sql, params)
        rows = None if write else cur.fetchall()
        if write:
            conn().commit()
        return rows, cur.rowcount
    import psycopg
    q, p = _translate(sql, len(params)), (params or None)
    for attempt in (0, 1):
        c = _live()
        try:
            cur = c.execute(q, p)
            return (None if write else cur.fetchall()), cur.rowcount
        except psycopg.OperationalError as exc:
            if not _connection_level(exc):
                raise
            close()
            if write or attempt:
                raise


def execute(sql: str, params: tuple = (), *, track: bool = True) -> int:
    """One write, committed. Returns the affected row count."""
    global dirty_since
    with _lock:
        _, n = _run(sql, params, True)
        if track and dirty_since is None and not is_postgres():
            dirty_since = time.monotonic()
        return n


def query(sql: str, params: tuple = ()) -> list[dict]:
    with _lock:
        return [dict(r) for r in _run(sql, params, False)[0]]


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
    if dirty_since is None and not is_postgres():
        dirty_since = time.monotonic()


def clear_dirty() -> None:
    global dirty_since
    dirty_since = None


EXPORT_TABLES = ("users", "invites", "accounts", "guest_turns", "guest_turns_by_user", "guest_issue", "r2_usage", "meta", "devices",
                 "pocket_conversations", "pocket_turns", "pocket_notes", "usage_events", "account_limits", "account_events")


def export_json() -> dict:
    """Every table as {name: [row, ...]}: what /admin/export ships on Postgres, where there is no file to copy."""
    return {t: query(f"SELECT * FROM {t}") for t in EXPORT_TABLES}


def backup(dest: Path) -> None:
    """A consistent copy of the live db (sqlite3 backup API), safe while writes continue."""
    if is_postgres():
        raise RuntimeError("backup() is SQLite-only: Postgres is durable, use the provider's backups")
    with _lock:
        out = sqlite3.connect(dest)
        try:
            conn().backup(out)
        finally:
            out.close()


def replace_with(snapshot: bytes, merge_local: bool) -> None:
    """Swap in a restored snapshot. With merge_local, rows written locally since (users,
    invites, devices, Pocket data, usage rows, account limits and events, guest counters and caps, R2 counters,
    missing meta) are folded into it first, so a late restore loses nothing."""
    if is_postgres():
        raise RuntimeError("replace_with() is SQLite-only")
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
                # A local-only account whose username or email the snapshot already uses is dropped whole:
                # its users row too, or it would be left behind with no account. (Its tokens are not merged.)
                c.execute("CREATE TEMP TABLE skip_ids AS SELECT l.user_id AS id FROM loc.accounts AS l "
                          "WHERE l.user_id NOT IN (SELECT user_id FROM main.accounts) AND "
                          "(l.username_lower IN (SELECT username_lower FROM main.accounts) "
                          "OR l.email_lower IN (SELECT email_lower FROM main.accounts))")
                # Rows on both sides: the newer updated_at wins; rows only local are added.
                for table, key in (("users", "id"), ("invites", "token"), ("accounts", "user_id")):
                    cols = [r[1] for r in c.execute(f"PRAGMA main.table_info({table})")]
                    lcols = {r[1] for r in c.execute(f"PRAGMA loc.table_info({table})")}
                    use = ", ".join(x for x in cols if x in lcols)
                    skip = f" AND {key} NOT IN (SELECT id FROM skip_ids)" if table in ("users", "accounts") else ""
                    c.execute(f"INSERT OR IGNORE INTO main.{table} ({use}) SELECT {use} FROM loc.{table} "
                              f"WHERE {key} NOT IN (SELECT {key} FROM main.{table}){skip}")
                    # consumed state (invites) only moves forward: it is excluded from the newer-wins copy
                    sets = ", ".join(f"{x} = l.{x}" for x in cols
                                     if x in lcols and x != key and not (table == "invites" and x in ("consumed", "consumed_by")))
                    c.execute(f"UPDATE main.{table} AS m SET {sets} FROM loc.{table} AS l "
                              f"WHERE m.{key} = l.{key} AND l.updated_at > m.updated_at")
                # Token state only moves forward: a spend or revocation made locally since the snapshot
                # must survive it (a revoked refresh family or a used reset link must not come back).
                for table in ("auth_tokens", "refresh_tokens"):
                    tcols = [r[1] for r in c.execute(f"PRAGMA main.table_info({table})")]
                    tl = {r[1] for r in c.execute(f"PRAGMA loc.table_info({table})")}
                    use = ", ".join(x for x in tcols if x in tl)
                    c.execute(f"INSERT OR IGNORE INTO main.{table} ({use}) SELECT {use} FROM loc.{table} "
                              "WHERE user_id NOT IN (SELECT id FROM skip_ids)")
                # Device keys: local-only devices are added; a revocation made locally stays revoked.
                dcols = [r[1] for r in c.execute("PRAGMA main.table_info(devices)")]
                dl = {r[1] for r in c.execute("PRAGMA loc.table_info(devices)")}
                use = ", ".join(x for x in dcols if x in dl)
                c.execute(f"INSERT OR IGNORE INTO main.devices ({use}) SELECT {use} FROM loc.devices "
                          "WHERE user_id NOT IN (SELECT id FROM skip_ids)")
                c.execute("UPDATE main.devices AS m SET revoked = 1 FROM loc.devices AS l "
                          "WHERE m.id = l.id AND l.revoked = 1")
                # Pocket data, usage rows and account events: rows only local are added (ids are random, so a row
                # present on both sides is the same row); a conversation ended locally stays ended.
                for table in ("pocket_conversations", "pocket_turns", "pocket_notes", "usage_events", "account_events"):
                    tcols = [r[1] for r in c.execute(f"PRAGMA main.table_info({table})")]
                    tl = {r[1] for r in c.execute(f"PRAGMA loc.table_info({table})")}
                    use = ", ".join(x for x in tcols if x in tl)
                    c.execute(f"INSERT OR IGNORE INTO main.{table} ({use}) SELECT {use} FROM loc.{table} "
                              "WHERE user_id IS NULL OR user_id NOT IN (SELECT id FROM skip_ids)")
                c.execute("UPDATE main.pocket_conversations AS m SET ended_at = l.ended_at FROM loc.pocket_conversations AS l "
                          "WHERE m.id = l.id AND m.ended_at IS NULL AND l.ended_at IS NOT NULL")
                # Per-account limits: the newer updated_at wins; accounts only local are added.
                lcols = [r[1] for r in c.execute("PRAGMA main.table_info(account_limits)")]
                ll = {r[1] for r in c.execute("PRAGMA loc.table_info(account_limits)")}
                use = ", ".join(x for x in lcols if x in ll)
                c.execute(f"INSERT OR IGNORE INTO main.account_limits ({use}) SELECT {use} FROM loc.account_limits "
                          "WHERE user_id NOT IN (SELECT id FROM skip_ids)")
                sets = ", ".join(f"{x} = l.{x}" for x in lcols if x in ll and x != "user_id")
                c.execute(f"UPDATE main.account_limits AS m SET {sets} FROM loc.account_limits AS l "
                          "WHERE m.user_id = l.user_id AND COALESCE(l.updated_at, '') > COALESCE(m.updated_at, '')")
                c.execute("UPDATE main.auth_tokens AS m SET consumed = 1 FROM loc.auth_tokens AS l "
                          "WHERE m.token_hash = l.token_hash AND l.consumed = 1")
                c.execute("UPDATE main.refresh_tokens AS m SET revoked = 1 FROM loc.refresh_tokens AS l "
                          "WHERE m.token_hash = l.token_hash AND l.revoked = 1")
                c.execute("UPDATE main.refresh_tokens AS m SET used = 1, used_at = l.used_at FROM loc.refresh_tokens AS l "
                          "WHERE m.token_hash = l.token_hash AND l.used = 1 AND m.used = 0")
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
