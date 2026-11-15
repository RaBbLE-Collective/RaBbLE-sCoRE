"""
Keeps the database across Render redeploys (the free tier's disk is ephemeral).

Never wipe data: a snapshot key is written once (If-None-Match: *) and never overwritten. Each
upload is snapshots/score-g<generation>-<UTC time>.db, then the small pointer snapshots/latest.json
(key, gen, writer, sha256, user/invite counts, protect list) is replaced with If-Match on the etag
we last read, which fences a second writer. Before uploading, the local copy must pass PRAGMA
integrity_check and must not have materially fewer users/invites than the baseline (the max of the
pointer's and the restored snapshot's counts; SNAPSHOT_ALLOW_SHRINK=1 allows a deliberate shrink).

Single writer: Render overlaps the old and new instance during a deploy (and `--workers N` would
be N writers in one deploy: run ONE worker). Both would restore the same pointer and upload. The
loser's conditional pointer PUT fails (412): it goes to state "failed", re-restores (merging its
local rows into the winner's lineage) and uploads the union; stop() does this once at shutdown.

Retention is tiered: the newest SNAPSHOT_KEEP (8) plus the first snapshot of each hour for 48 h
(SNAPSHOT_KEEP_HOURLY_H) and of each day for 90 d (SNAPSHOT_KEEP_DAILY_D). Keys newer than a
fallback restore's source are protected (pointer["protect"]) and never pruned.

Boot: R2 configured and no local score.db, read the pointer, download and verify (integrity
check, sha256), retrying the pointer's object (SNAPSHOT_POINTER_RETRIES) before anything else. A
404 or a corrupt object never means "start fresh". If the pointer's object stays unusable, the
next older snapshot is restored as a FALLBACK and uploads go on HOLD (persisted in .restore-hold)
until an admin checks R2 and sets SNAPSHOT_ACCEPT_FALLBACK=<the restored key>. Finding nothing
is NOT a fresh start by itself (a wrong prefix, bucket or token scope looks the same):
it needs R2_ALLOW_FRESH_START=1 on that boot, a bucket with no snapshot-looking object anywhere,
and no snapshots/INITIALIZED marker (written after the first upload). Otherwise, like any other
failure, uploads stay off (state "failed") and the restore retries every SNAPSHOT_RESTORE_RETRY_S.
A db on disk whose pointer cannot be read is on hold too. Running: after writes, upload
debounced (SNAPSHOT_DEBOUNCE_S, 30), only if changed. Shutdown: one last flush (after one
re-merge if the restore had failed or we were fenced). A crash loses the last few seconds of
writes: accepted. Every request is counted (storage.py).
"""
import asyncio
import hashlib
import json
import logging
import os
import re
import sqlite3
import tempfile
import threading
import time
import uuid
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
fallback_hold: dict | None = None        # a degraded restore awaiting SNAPSHOT_ACCEPT_FALLBACK=<source key>
_protect: set[str] = set()               # keys newer than a fallback source: never pruned (carried in the pointer)
INSTANCE = uuid.uuid4().hex[:8]          # this process, written into every pointer
_UNKNOWN = "?"
_ptr_etag = _UNKNOWN                     # md5 of the pointer object as last read/written; "" = absent
_last_put_key: str | None = None         # the snapshot key our latest pointer PUT named (self-fence check)
LINEAGE = "snapshot_lineage"             # meta: the pointer key this db was last restored from or uploaded as


def _pending_file() -> Path:
    return db.db_path().with_name(".restore-pending")   # survives a restart: a failed boot stays failed


def _hold_file() -> Path:
    return db.db_path().with_name(".restore-hold")


def allow_fresh() -> bool:
    return os.getenv("R2_ALLOW_FRESH_START", "") in ("1", "true", "yes")


def uploads_enabled() -> bool:
    return state != "failed" and hold is None and fallback_hold is None


def status() -> dict:
    return {"state": state, "allow_fresh_start": allow_fresh(), "hold": hold, "fallback_hold": fallback_hold, "baseline": dict(baseline), "latest": dict(latest),
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



def _md5(raw: bytes) -> str:
    return hashlib.md5(raw).hexdigest()            # the S3/R2 etag of a single-part PUT


def _gen(key: str | None) -> int:
    m = re.search(r"score-g(\d+)-", key or "")
    return int(m.group(1)) if m else 0             # legacy names (score-<timestamp>.db) are generation 0


def _ts(key: str) -> datetime | None:
    m = re.search(r"(\d{8}T\d{6}Z)", key)
    try:
        return datetime.strptime(m.group(1), "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc) if m else None
    except ValueError:
        return None


def _try_pointer(f: _Fetch, key: str, want: str | None):
    """Fetch and verify one snapshot object. (data, users, invites) or (None, reason)."""
    data = f.get(key)
    if data is None:
        return None, "missing"
    ok, users, invites = inspect(data)
    if not ok or (want and hashlib.sha256(data).hexdigest() != want):
        return None, "corrupt"
    return data, users, invites


def _find_snapshot(f: _Fetch):
    """The newest snapshot that verifies: the pointer's first (retried: a 404 is not proof), then
    older ones newest first. Returns (data, key, (users, invites), info) or None when the bucket
    holds no snapshot at all. info["fallback"] is a reason string when we restored from anything
    other than the pointer's object (a degraded restore: the caller must HOLD uploads).
    Raises if there are snapshots but none verifies."""
    global _known, latest, _ptr_etag
    tried, bad = [], []
    pointer, pstate = None, "ok"
    raw = f.get(POINTER_KEY)
    _ptr_etag = "" if raw is None else _md5(raw)
    if raw is None:
        pstate = "missing"
    else:
        try:
            pointer = json.loads(raw)
            assert isinstance(pointer["key"], str) and pointer["key"].startswith(SNAP_PREFIX)
        except Exception:
            logger.error("snapshot pointer is corrupt: falling back to the newest snapshot")
            pointer, pstate = None, "corrupt"
    keys = None
    if pointer:
        tried.append(pointer["key"])
        for attempt in range(_retries() + 1):
            got = _try_pointer(f, pointer["key"], pointer.get("sha256"))
            if got[0] is not None:
                data, users, invites = got
                latest = pointer
                return data, pointer["key"], (users, invites), {"pointer": pointer, "fallback": None, "skipped": []}
            if attempt < _retries():
                time.sleep(_retry_sleep())
        bad.append(f"{pointer['key']}: {got[1]}")
        logger.error("snapshot %s is %s after %d tries: trying an older one", pointer["key"], got[1], _retries() + 1)
    keys = sorted((k for k in f.list(SNAP_PREFIX) if k.endswith(".db")), reverse=True)
    for key in [k for k in keys if k not in tried]:
        tried.append(key)
        got = _try_pointer(f, key, None)
        if got[0] is None:
            bad.append(f"{key}: {got[1]}")
            continue
        data, users, invites = got
        _known = sorted(keys)
        skipped = sorted({k for k in keys if k > key} | ({pointer["key"]} if pointer else set()))
        why = (f"restored from {key} but the pointer names {pointer['key']} ({'; '.join(bad)})" if pointer
               else f"restored from {key} with the pointer {pstate}")
        logger.warning("FALLBACK RESTORE: %s", why)
        latest = dict(pointer) if pointer else {"key": key}
        return data, key, (users, invites), {"pointer": pointer, "fallback": why, "skipped": skipped}
    if not tried:
        return None                                # nothing in the bucket: a fresh start
    raise RuntimeError("no snapshot verified (" + "; ".join(bad) + ")")


def _retries() -> int:
    return max(0, int(os.getenv("SNAPSHOT_POINTER_RETRIES", "3")))


def _retry_sleep() -> float:
    return float(os.getenv("SNAPSHOT_POINTER_RETRY_S", "1"))


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


def _acked(key: str) -> bool:
    ack = os.getenv("SNAPSHOT_ACCEPT_FALLBACK", "").strip()
    return bool(ack) and ack.rsplit("/", 1)[-1] == key.rsplit("/", 1)[-1]


def _clear_hold() -> None:
    _hold_file().unlink(missing_ok=True)
    try:
        db.execute("DELETE FROM meta WHERE key = 'fallback_hold'", track=False)
    except Exception as exc:
        logger.warning("fallback hold copy in the db not cleared: %s", exc)


def _hold_fallback(source: str, why: str, skipped: list[str]) -> None:
    """A degraded restore: uploads stay off until an admin acks with SNAPSHOT_ACCEPT_FALLBACK=<source>.
    The hold is written to a file and into the db's meta; if neither lands, the state goes "failed"
    (uploads off, restore retried) rather than carrying on unheld."""
    global fallback_hold, state
    fallback_hold = {"source": source, "reason": why, "protect": list(skipped)}
    body = json.dumps(fallback_hold)
    persisted = False
    try:
        _hold_file().write_text(body)
        persisted = True
    except OSError as exc:
        logger.error("fallback hold file not written: %s", exc)
    try:
        db.meta_set("fallback_hold", body)
        persisted = True
    except Exception as exc:
        logger.error("fallback hold not stored in the db either: %s", exc)
    if not persisted:
        state = "failed"
        logger.critical("FALLBACK HOLD NOT PERSISTED anywhere: uploads stay OFF (state failed) so a restart "
                        "cannot resume them unheld")
    logger.error("UPLOADS ON HOLD: %s. Newer snapshots are kept untouched. Inspect R2, then set "
                 "SNAPSHOT_ACCEPT_FALLBACK=%s and redeploy to accept this restore", why, source)
    _apply_ack()


def _apply_ack() -> None:
    global fallback_hold, _protect
    if fallback_hold and _acked(fallback_hold["source"]):
        _protect |= set(fallback_hold.get("protect", []))   # carried in the pointer from now on
        logger.warning("fallback restore from %s ACCEPTED by SNAPSHOT_ACCEPT_FALLBACK: uploads on; "
                       "newer keys stay protected from pruning", fallback_hold["source"])
        fallback_hold = None
        _clear_hold()


def try_restore() -> str:
    """Boot restore, and the retry after a failed one. Returns the new state."""
    global state, baseline, _known, fallback_hold
    if not storage.configured():
        state = "disabled"
        return state
    local = db.db_path().exists()
    if local and state != "failed" and _pending_file().exists():
        state = "failed"                          # an earlier boot failed its restore: still unresolved
    if local and state != "failed":
        state = "present"
        _load_hold_file()
        _load_baseline()
        why = _diverged()
        if not why:
            return state
        state = "failed"                          # not a descendant of the pointer: merge, never upload as is
        logger.error("LOCAL DB DOES NOT DESCEND FROM THE POINTER (%s): restoring and merging it", why)
        _pending_file().write_text(f"diverged: {why}")
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
        db.db_path().parent.mkdir(parents=True, exist_ok=True)
        _pending_file().write_text(str(exc))      # before settle(): that opens (creates) the local db
        f.settle()
        return state
    if found is None:
        state = "fresh"
        baseline = {"users": 0, "invites": 0}
        logger.warning("FRESH START (R2_ALLOW_FRESH_START=1, empty bucket, no INITIALIZED marker): uploads on. "
                       "Unset the flag after this boot.")
    else:
        data, key, (users, invites), info = found
        if not local:                              # keep the marker ahead of the db it describes
            db.db_path().parent.mkdir(parents=True, exist_ok=True)
            _pending_file().write_text("restore in progress")
        db.replace_with(data, merge_local=local)
        state = "restored"
        p = info["pointer"] or {}
        # The pointer carries the newest counts even when its object is unusable: never go below them.
        baseline = {"users": max(users, int(p.get("users", 0))), "invites": max(invites, int(p.get("invites", 0)))}
        logger.info("restored database from R2 snapshot %s (%d bytes)%s", key, len(data),
                    ", local writes merged" if local else "")
        if local:
            db.mark_dirty()                       # the merged db goes back up
        if info["fallback"]:
            _hold_fallback(key, info["fallback"], info["skipped"])
        else:
            fallback_hold = None
            _clear_hold()
        _protect.update(p.get("protect", []))
        try:
            db.meta_set(LINEAGE, p.get("key") or key)   # a fallback descends from the pointer it stands in for
        except Exception as exc:
            logger.error("lineage not recorded: %s", exc)
    f.settle()
    _pending_file().unlink(missing_ok=True)
    return state


def _diverged() -> str | None:
    """Why a db already on disk may not upload as is: it was not last restored from / uploaded as the
    pointer's key (a stale disk, a laptop with prod env, a legacy db with no lineage). None when it
    descends from the pointer, or when there is nothing to compare (held, pointer absent)."""
    if hold or fallback_hold or _ptr_etag in (_UNKNOWN, "") or not latest.get("key"):
        return None
    mine = db.meta_get(LINEAGE)
    if mine == latest["key"]:
        return None
    return f"db lineage {mine or 'unknown'}, pointer {latest['key']}"


def _load_hold_file() -> None:
    """A fallback hold survives a restart on the same disk until an admin acks it."""
    global fallback_hold
    try:
        if _hold_file().exists():
            fallback_hold = json.loads(_hold_file().read_text())
        elif db.meta_get("fallback_hold"):
            fallback_hold = json.loads(db.meta_get("fallback_hold"))
        if fallback_hold:
            _apply_ack()
    except Exception as exc:
        logger.error("fallback hold file unreadable (%s): holding uploads", exc)
        fallback_hold = {"source": "unknown", "reason": "hold file unreadable", "protect": []}


def _load_baseline() -> None:
    """A db already on disk at boot: learn the last snapshot's counts for the shrink gate. An
    unreadable pointer is a HOLD (the gate has no baseline), not a baseline of zero."""
    global baseline, latest, _ptr_etag, hold
    try:
        raw = storage.get(POINTER_KEY)
        if raw is None:
            _ptr_etag = ""
            if storage.get(MARKER_KEY) is not None:
                raise RuntimeError("snapshots/latest.json is missing but the bucket was initialised")
            return
        p = json.loads(raw)
        baseline = {"users": max(baseline["users"], int(p.get("users", 0))),
                    "invites": max(baseline["invites"], int(p.get("invites", 0)))}
        latest, _ptr_etag = p, _md5(raw)
        _protect.update(p.get("protect", []))
    except Exception as exc:
        _ptr_etag = _UNKNOWN
        hold = f"snapshot pointer not readable ({exc}): the shrink gate and the fence have no baseline"
        logger.error("UPLOADS ON HOLD: %s", hold)


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


def _ensure_known() -> list[str]:
    global _known
    if _known is None:
        _known = sorted(k for k in (i["key"] for i in storage.list_keys(SNAP_PREFIX)) if k.endswith(".db"))
    return _known


def _fenced(exc: Exception) -> None:
    """Another writer moved the pointer (a deploy overlap, a second instance): stop uploading and
    re-merge from R2 (state "failed" makes the loop, or stop(), run try_restore, which folds our
    local rows into the winner's lineage and uploads the union)."""
    global state
    state = "failed"
    try:
        _pending_file().write_text(f"fenced: {exc}")
    except OSError:
        pass
    logger.error("SNAPSHOT FENCED: another writer owns the pointer (%s); uploads off until re-merged", exc)


def _put_pointer(key: str, body: bytes, pointer: dict) -> None:
    """Move the pointer under the fence. A 412 whose current pointer was written by THIS instance
    and names the key of our last PUT means that PUT landed and only its response was lost: adopt
    its etag and go on (once), instead of fencing ourselves. Anything else is a real fence."""
    global _ptr_etag, _last_put_key
    prior = _last_put_key
    _last_put_key = key
    try:
        if _ptr_etag == "":
            storage.put(POINTER_KEY, body, "application/json", if_none_match="*")
        else:
            storage.put(POINTER_KEY, body, "application/json", if_match=_ptr_etag)   # the fence
    except storage.PreconditionFailed:
        raw = storage.get(POINTER_KEY)
        try:
            cur = json.loads(raw) if raw else {}
        except ValueError:
            cur = {}
        if not (cur.get("writer") == INSTANCE and prior and cur.get("key") == prior):
            raise
        logger.warning("pointer PUT %s had landed (response lost): adopting its etag", prior)
        _ptr_etag = _md5(raw)
        pointer["protect"] = sorted(set(pointer.get("protect", [])) | set(cur.get("protect", [])))
        if not pointer["protect"]:
            pointer.pop("protect")
        body = json.dumps(pointer).encode()
        storage.put(POINTER_KEY, body, "application/json", if_match=_ptr_etag)
    _ptr_etag = _md5(body)


def _flush() -> bool:
    global hold, baseline, latest, _known, _ptr_etag, state
    if (db.dirty_since is None or not storage.configured() or state == "failed"
            or storage.exhausted()):
        return False
    if fallback_hold:
        _apply_ack()
        if fallback_hold:
            return False                           # degraded restore: an admin must ack first
    if _ptr_etag == _UNKNOWN:
        _load_baseline()
        if _ptr_etag == _UNKNOWN:
            return False
        hold = None
        why = _diverged()                          # the boot-time lineage check was skipped while held
        if why:
            state = "failed"                       # not a descendant of the pointer: merge, never upload
            logger.error("LOCAL DB DOES NOT DESCEND FROM THE POINTER (%s): restoring and merging it", why)
            try:
                _pending_file().write_text(f"diverged: {why}")
            except OSError as exc:
                logger.error("pending marker not written: %s", exc)
            return False
    db.clear_dirty()   # writes landing during the upload re-mark it
    try:
        with tempfile.TemporaryDirectory() as d:
            copy = Path(d) / "snap.db"
            db.backup(copy)
            data = copy.read_bytes()
    except Exception as exc:
        logger.error("snapshot copy failed: %s", exc)
        db.mark_dirty()
        return False
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
    try:
        gen = max([_gen(k) for k in _ensure_known()] + [_gen(latest.get("key")), int(latest.get("gen", 0))]) + 1
    except Exception as exc:
        logger.warning("snapshot skipped (cannot list existing generations): %s", exc)
        db.mark_dirty()
        return False
    now = datetime.now(timezone.utc)
    key = f"{SNAP_PREFIX}g{gen:08d}-{now.strftime('%Y%m%dT%H%M%SZ')}.db"
    pointer = {"key": key, "gen": gen, "writer": INSTANCE, "at": now.isoformat(), "bytes": len(data),
               "users": users, "invites": invites, "sha256": hashlib.sha256(data).hexdigest()}
    protect = sorted(_protect | set(latest.get("protect", [])))
    if protect:
        pointer["protect"] = protect
    body = json.dumps(pointer).encode()
    try:
        storage.put(key, data, "application/vnd.sqlite3", if_none_match="*")   # create-only: never overwritten
        _known.append(key)
        _put_pointer(key, body, pointer)
        baseline, latest = {"users": max(users, 0), "invites": invites}, pointer
        db.meta_set("last_snapshot", now.isoformat())
        db.meta_set(LINEAGE, key)
    except storage.PreconditionFailed as exc:
        _fenced(exc)
        db.mark_dirty()
        return False
    except storage.Unavailable as exc:
        logger.warning("snapshot skipped: %s", exc)
        db.mark_dirty()
        return False
    except Exception as exc:
        logger.error("snapshot upload failed: %s", exc)
        db.mark_dirty()
        return False
    try:   # after the snapshot, so it includes the snapshot's own put; the snapshot is up either way
        rows = db.query("SELECT month, class_a, class_b FROM r2_usage")
        storage.put(storage.USAGE_KEY, json.dumps({r["month"]: {"class_a": r["class_a"], "class_b": r["class_b"]}
                                                   for r in rows}).encode(), "application/json")
    except Exception as exc:
        logger.warning("usage object not uploaded: %s", exc)
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


def _keep_set(known: list[str], now: datetime) -> set[str]:
    """Tiered retention: the newest SNAPSHOT_KEEP (8), plus the first snapshot of each hour for
    SNAPSHOT_KEEP_HOURLY_H (48) hours and of each day for SNAPSHOT_KEEP_DAILY_D (90) days.
    Names with no parseable timestamp are always kept."""
    keep = set(known[-_keep():])
    hours = int(os.getenv("SNAPSHOT_KEEP_HOURLY_H", "48"))
    days = int(os.getenv("SNAPSHOT_KEEP_DAILY_D", "90"))
    seen_h, seen_d = set(), set()
    for k in known:                                # oldest first: the first of a bucket wins
        t = _ts(k)
        if t is None:
            keep.add(k)
            continue
        if now - t <= timedelta(hours=hours) and t.strftime("%Y%m%d%H") not in seen_h:
            seen_h.add(t.strftime("%Y%m%d%H"))
            keep.add(k)
        if now - t <= timedelta(days=days) and t.strftime("%Y%m%d") not in seen_d:
            seen_d.add(t.strftime("%Y%m%d"))
            keep.add(k)
    return keep


def _prune(newest: str) -> None:
    """Drop snapshots outside the retention tiers. Only after a newer upload succeeded; never the
    pointer's object, the one just uploaded, or any key a fallback restore skipped (_protect)."""
    try:
        known = _ensure_known()
        if newest not in known:
            known.append(newest)
        known.sort()
        protected = {newest, latest.get("key")} | _protect | set(latest.get("protect", []))
        keep = _keep_set(known, datetime.now(timezone.utc))
        for k in [k for k in known if k not in keep and k not in protected]:
            storage.delete(k)
            known.remove(k)
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
        for _ in range(2):                         # a failed or fenced writer merges its rows back before the end
            if state == "failed":
                await asyncio.to_thread(try_restore)
            await asyncio.to_thread(flush)
            if state != "failed":
                break
