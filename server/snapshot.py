"""
Keeps the database across Render redeploys (the free tier's disk is ephemeral).

Never wipe data: a snapshot key is written once and never overwritten. Each upload is
snapshots/score-YYYYMMDDTHHMMSSZ.db, then the small pointer snapshots/latest.json
(key, sha256, user/invite counts) is updated, then the oldest beyond SNAPSHOT_KEEP (8)
are pruned, never the pointer's object nor the one just uploaded. Before uploading, the
local copy must pass PRAGMA integrity_check and must not have materially fewer
users/invites than the last restored/uploaded snapshot (SNAPSHOT_ALLOW_SHRINK=1 allows it).

Boot: R2 configured and no local score.db, read the pointer, download and verify (integrity
check, sha256), falling back to the next newest snapshot if it is missing or corrupt. Finding
nothing is NOT a fresh start by itself (a wrong prefix, bucket or token scope looks the same):
it needs R2_ALLOW_FRESH_START=1 on that boot, a bucket with no snapshot-looking object anywhere,
and no snapshots/INITIALIZED marker (written after the first upload). Otherwise, like any other
failure, uploads stay off (state "failed") and the restore retries every SNAPSHOT_RESTORE_RETRY_S. Running: after writes, upload
debounced (SNAPSHOT_DEBOUNCE_S, 30), only if changed. Shutdown: one last flush. A crash loses
the last few seconds of writes: accepted. Every request is counted (storage.py).
"""
import asyncio
import hashlib
import json
import logging
import os
import sqlite3
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import db
import storage

logger = logging.getLogger("rabble.snapshot")

SNAP_PREFIX = "snapshots/score-"
POINTER_KEY = "snapshots/latest.json"
MARKER_KEY = "snapshots/INITIALIZED"
DEBOUNCE_S = float(os.getenv("SNAPSHOT_DEBOUNCE_S", "30"))
LOG_EVERY_S = 600
_task: asyncio.Task | None = None


# Restore state. Uploads stay off while it is "failed": an empty local db must never replace
# the copy in R2. "present" (db already on disk), "restored" and "fresh" (R2 holds no snapshot
# at all) allow uploads. "disabled" is local-only mode.
state = "disabled"
hold: str | None = None                  # why uploads are paused by the sanity gate, if they are
baseline = {"users": 0, "invites": 0}    # counts of the last restored/uploaded snapshot
latest: dict = {}                        # the pointer as last read or written
RETRY_S = float(os.getenv("SNAPSHOT_RESTORE_RETRY_S", "300"))
_flush_lock = threading.Lock()
_known: list[str] | None = None          # snapshot keys we know of (listed once, then tracked)
_marker_done = False                     # INITIALIZED written (or seen) by this process


def _pending_file() -> Path:
    return db.db_path().with_name(".restore-pending")   # survives a restart: a failed boot stays failed


def allow_fresh() -> bool:
    return os.getenv("R2_ALLOW_FRESH_START", "") in ("1", "true", "yes")


def uploads_enabled() -> bool:
    return state != "failed" and hold is None


def status() -> dict:
    return {"state": state, "allow_fresh_start": allow_fresh(), "hold": hold, "baseline": dict(baseline), "latest": dict(latest),
            "keep": _keep(), "uploads_enabled": uploads_enabled() and storage.configured()}


def _keep() -> int:
    return max(1, int(os.getenv("SNAPSHOT_KEEP", "8")))


def inspect(data: bytes) -> tuple[bool, int, int]:
    """(usable, users, invites): the bytes open as sqlite, pass integrity_check and hold our tables."""
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "x.db"
        p.write_bytes(data)
        try:
            c = sqlite3.connect(p)
            try:
                if c.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    return False, 0, 0
                return (True, c.execute("SELECT COUNT(*) FROM users").fetchone()[0],
                        c.execute("SELECT COUNT(*) FROM invites").fetchone()[0])
            finally:
                c.close()
        except sqlite3.Error:
            return False, 0, 0


class _Fetch:
    """Reads from R2. With no local db yet, nothing may open (and so create) the db before the
    answer is known, so requests go straight to the client and are counted afterwards (settle)."""

    def __init__(self, counted: bool):
        self.counted, self.a, self.b = counted, 0, 0

    def get(self, key: str) -> bytes | None:
        if self.counted:
            return storage.get(key)
        self.b += 1
        return storage._get_client().get(storage.prefix() + key)

    def list(self, prefix: str) -> list[str]:
        if self.counted:
            return [i["key"] for i in storage.list_keys(prefix)]
        self.a += 1
        n = len(storage.prefix())
        return [i["key"][n:] for i in storage._get_client().list(storage.prefix() + prefix)]

    def list_root(self) -> list[str]:
        """Every key in the bucket, whatever the prefix (counted like any LIST)."""
        if self.counted:
            storage.record("class_a")
        else:
            self.a += 1
        return [i["key"] for i in storage._get_client().list("")]

    def settle(self) -> None:
        for _ in range(self.a):
            storage.record("class_a")
        for _ in range(self.b):
            storage.record("class_b")


def _find_snapshot(f: _Fetch) -> tuple[bytes, str, tuple[int, int]] | None:
    """The newest snapshot that verifies: the pointer's first, then older ones newest first.
    None = the bucket holds no snapshot at all. Raises if there are snapshots but none verifies."""
    global _known, latest
    tried, bad = [], []
    pointer = None
    raw = f.get(POINTER_KEY)
    if raw is not None:
        try:
            pointer = json.loads(raw)
            assert isinstance(pointer["key"], str) and pointer["key"].startswith(SNAP_PREFIX)
        except Exception:
            logger.error("snapshot pointer is corrupt: falling back to the newest snapshot")
            pointer = None
    keys = None
    for attempt in ("pointer", "listing"):
        if attempt == "pointer":
            cands = [pointer["key"]] if pointer else []
        else:
            keys = sorted((k for k in f.list(SNAP_PREFIX) if k.endswith(".db")), reverse=True)
            cands = [k for k in keys if k not in tried]
        for key in cands:
            tried.append(key)
            data = f.get(key)
            if data is None:
                bad.append(f"{key}: missing")
                continue
            ok, users, invites = inspect(data)
            want = pointer.get("sha256") if pointer and key == pointer["key"] else None
            if not ok or (want and hashlib.sha256(data).hexdigest() != want):
                bad.append(f"{key}: corrupt")
                logger.error("snapshot %s is corrupt: trying an older one", key)
                continue
            if bad:
                logger.warning("restored from %s after skipping: %s", key, "; ".join(bad))
            _known = sorted(keys or [], reverse=False) if keys is not None else None
            latest = pointer if pointer and key == pointer["key"] else {"key": key}
            return data, key, (users, invites)
    if keys is None and not tried:
        keys = []
    if not tried:
        return None                                # nothing in the bucket: a fresh start
    raise RuntimeError("no snapshot verified (" + "; ".join(bad) + ")")


def _fresh_refusal(f: _Fetch) -> str | None:
    """Why an empty bucket may NOT be accepted as a first boot, or None when it may."""
    if not allow_fresh():
        return ("no snapshot found in R2 (a wrong R2_PREFIX or R2_BUCKET, a mis-scoped token or a deleted "
                "object looks the same); to start fresh on purpose set R2_ALLOW_FRESH_START=1 for one boot")
    if f.get(MARKER_KEY) is not None:
        return ("snapshots/INITIALIZED exists: this bucket held a database before, so a missing snapshot "
                "is never a fresh start; an admin must delete the marker deliberately")
    seen = [k for k in f.list_root() if "snapshot" in k.lower() and (k.endswith(".db") or k.endswith("latest.json"))]
    if seen:
        return f"R2 holds snapshot-looking objects ({', '.join(seen[:3])}): refusing to start fresh"
    return None


def try_restore() -> str:
    """Boot restore, and the retry after a failed one. Returns the new state."""
    global state, baseline, _known
    if not storage.configured():
        state = "disabled"
        return state
    local = db.db_path().exists()
    if local and state != "failed" and _pending_file().exists():
        state = "failed"                          # an earlier boot failed its restore: still unresolved
    if local and state != "failed":
        state = "present"
        _load_baseline()
        return state
    f = _Fetch(counted=local)
    try:
        found = _find_snapshot(f)
        if found is None:
            refusal = _fresh_refusal(f)
            if refusal:
                raise RuntimeError(refusal)
    except Exception as exc:                      # includes Unavailable (budget)
        state = "failed"
        logger.error("SNAPSHOT RESTORE FAILED (%s): serving from a local db, R2 uploads DISABLED "
                     "until a restore succeeds; retrying every %ds", exc, RETRY_S)
        f.settle()                                # opens a fresh local db; the requests still cost
        db.db_path().parent.mkdir(parents=True, exist_ok=True)
        _pending_file().write_text(str(exc))
        return state
    if found is None:
        state = "fresh"
        baseline = {"users": 0, "invites": 0}
        logger.warning("FRESH START (R2_ALLOW_FRESH_START=1, empty bucket, no INITIALIZED marker): uploads on. "
                       "Unset the flag after this boot.")
    else:
        data, key, (users, invites) = found
        db.replace_with(data, merge_local=local)
        state = "restored"
        baseline = {"users": users, "invites": invites}
        logger.info("restored database from R2 snapshot %s (%d bytes)%s", key, len(data),
                    ", local writes merged" if local else "")
        if local:
            db.mark_dirty()                       # the merged db goes back up
    f.settle()
    _pending_file().unlink(missing_ok=True)
    return state


def _load_baseline() -> None:
    """A db already on disk at boot: learn the last snapshot's counts for the shrink gate."""
    global baseline, latest
    try:
        raw = storage.get(POINTER_KEY)
        p = json.loads(raw) if raw else {}
        baseline = {"users": int(p.get("users", 0)), "invites": int(p.get("invites", 0))}
        latest = p
    except Exception as exc:
        logger.warning("snapshot pointer not read (shrink gate has no baseline): %s", exc)


def load_usage() -> None:
    """Fold the usage object in R2 into the db (the restored db can lag it)."""
    if not storage.configured():
        return
    try:
        raw = storage.get(storage.USAGE_KEY)
        for month, u in (json.loads(raw).items() if raw else []):
            storage.merge_usage(month, int(u.get("class_a", 0)), int(u.get("class_b", 0)))
    except Exception as exc:
        logger.warning("usage object not loaded: %s", exc)


def flush() -> bool:
    """Upload a snapshot now if the db changed. True if one went up."""
    with _flush_lock:   # the shutdown flush waits for one still on the wire
        return _flush()


def _shrunk(users: int, invites: int) -> str | None:
    """Reason when the local counts are materially below the last snapshot's (more than 10% down)."""
    for name, n in (("users", users), ("invites", invites)):
        base = baseline[name]
        if n < base - base // 10:
            return f"local db has {n} {name}, last snapshot had {base}"
    return None


def _flush() -> bool:
    global hold, baseline, latest, _known
    if (db.dirty_since is None or not storage.configured() or state == "failed"
            or storage.exhausted()):
        return False
    db.clear_dirty()   # writes landing during the upload re-mark it
    with tempfile.TemporaryDirectory() as d:
        copy = Path(d) / "snap.db"
        db.backup(copy)
        data = copy.read_bytes()
    ok, users, invites = inspect(data)             # integrity_check on the copy we are about to send
    reason = None if ok else "local db failed integrity_check"
    if ok and os.getenv("SNAPSHOT_ALLOW_SHRINK", "") not in ("1", "true", "yes"):
        reason = _shrunk(users, invites)
    if reason:
        if hold != reason:
            logger.error("SNAPSHOT UPLOAD REFUSED, uploads on hold: %s (set SNAPSHOT_ALLOW_SHRINK=1 to "
                         "allow a deliberate shrink)", reason)
        hold = reason
        db.mark_dirty()
        return False
    hold = None
    now = datetime.now(timezone.utc)
    used = set(_known or []) | {latest.get("key")}
    while f"{SNAP_PREFIX}{now.strftime('%Y%m%dT%H%M%SZ')}.db" in used:   # a key is never reused
        now += timedelta(seconds=1)
    key = f"{SNAP_PREFIX}{now.strftime('%Y%m%dT%H%M%SZ')}.db"
    pointer = {"key": key, "at": now.isoformat(), "bytes": len(data), "users": users,
               "invites": invites, "sha256": hashlib.sha256(data).hexdigest()}
    try:
        storage.put(key, data, "application/vnd.sqlite3")          # a new key: nothing is overwritten
        storage.put(POINTER_KEY, json.dumps(pointer).encode(), "application/json")
        baseline, latest = {"users": users, "invites": invites}, pointer
        db.meta_set("last_snapshot", now.isoformat())
        # The usage object goes after the snapshot, so it includes the snapshot's own put.
        rows = db.query("SELECT month, class_a, class_b FROM r2_usage")
        storage.put(storage.USAGE_KEY, json.dumps({r["month"]: {"class_a": r["class_a"], "class_b": r["class_b"]}
                                                   for r in rows}).encode(), "application/json")
    except storage.Unavailable as exc:
        logger.warning("snapshot skipped: %s", exc)
        db.mark_dirty()
        return False
    except Exception as exc:
        logger.error("snapshot upload failed: %s", exc)
        db.mark_dirty()
        return False
    logger.info("snapshot uploaded: %s (%d bytes)", key, len(data))
    _mark_initialized()
    _prune(key)
    return True


def _mark_initialized() -> None:
    """After an upload succeeded the bucket has a history: from now on a missing pointer can
    never be a fresh start, flag or not."""
    global _marker_done
    if _marker_done:
        return
    try:
        storage.put(MARKER_KEY, now_iso().encode(), "text/plain")
        _marker_done = True
    except Exception as exc:
        logger.warning("INITIALIZED marker not written: %s", exc)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _prune(newest: str) -> None:
    """Drop snapshots beyond SNAPSHOT_KEEP, oldest first. Only after a newer upload succeeded,
    and never the pointer's object or the one just uploaded."""
    global _known
    try:
        if _known is None:
            _known = sorted(k for k in (i["key"] for i in storage.list_keys(SNAP_PREFIX)) if k.endswith(".db"))
        if newest not in _known:
            _known.append(newest)
        _known.sort()
        protected = {newest, latest.get("key")}
        extra = [k for k in _known[:-_keep()] if k not in protected]
        for k in extra:
            storage.delete(k)
            _known.remove(k)
            logger.info("pruned old snapshot %s", k)
    except Exception as exc:                       # pruning is housekeeping: never fails the upload
        logger.warning("snapshot prune skipped: %s", exc)


async def _loop() -> None:
    last_log = last_try = time.monotonic()
    while True:
        await asyncio.sleep(5)
        try:
            if state == "failed" and time.monotonic() - last_try >= RETRY_S:
                last_try = time.monotonic()
                if await asyncio.to_thread(try_restore) != "failed":
                    await asyncio.to_thread(load_usage)
            d = db.dirty_since
            if d is not None and time.monotonic() - d >= DEBOUNCE_S:
                await asyncio.to_thread(flush)
            if time.monotonic() - last_log >= LOG_EVERY_S:
                last_log = time.monotonic()
                storage.log_usage()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("snapshot loop")


async def start() -> None:
    """Call at app startup, before anything touches the db."""
    global _task
    global state
    if not storage.configured():
        state = "disabled"
        logger.info("R2 not configured: local-only mode, no snapshots")
        return
    if await asyncio.to_thread(try_restore) != "failed":
        await asyncio.to_thread(load_usage)
    _task = asyncio.create_task(_loop())


async def stop() -> None:
    """Call at graceful shutdown: cancel the loop, flush once."""
    global _task
    if _task:
        _task.cancel()
        _task = None
    if storage.configured():
        await asyncio.to_thread(flush)
