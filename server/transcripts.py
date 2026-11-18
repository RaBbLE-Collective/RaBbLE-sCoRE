"""
Transcript persistence — RaBbLE training data capture. /chat and session turns only.

Backends (TRANSCRIPT_BACKEND): auto (default: r2 when R2 is configured, else file), r2, file.
  r2   — entries buffer in memory and flush as ONE object per flush interval per day:
          transcripts/YYYY-MM-DD/<utc-ts>-<id>.jsonl (one JSON entry per line), via storage.py
          (counted against the monthly budget). Render's disk is wiped on every restart, so a
          local file is no protection; R2 is.
  file — DATA_DIR/transcripts/YYYY-MM-DD.jsonl, written immediately.

Flush: every TRANSCRIPT_FLUSH_S (default 60) when something is buffered, on graceful shutdown
(stop(), from the app lifespan), and early when the buffer passes TRANSCRIPT_BUFFER_MAX entries
(default 500) or TRANSCRIPT_BUFFER_BYTES (default 4 MB). Request cost: at most one PUT per flush per
UTC day touched, so <= 60 PUTs/hour however many conversations are active (one on a quiet hour).

Loss window: a hard crash (SIGKILL, OOM, power) loses what is still buffered: up to one flush
interval. SIGTERM (Render redeploy) flushes first.

Failure: a failed PUT leaves entries in the buffer and retries on the next flush. Never dropped
silently: when R2 is over budget, or the buffer is over its cap, or shutdown cannot reach R2,
the entries spill to the local file (last resort) with an error in the log. A failed or missing
read never matters here: transcripts are write-only.

Privacy contract:
  - session_id is a random UUID generated per conversation — never the user_id
  - No identity fields (user_id, IP, handle) are stored
  - Only message content, tier, workflow type, and timestamp are captured
"""

import asyncio
import json
import logging
import os
import threading
import uuid
from datetime import datetime, timezone

import storage

logger = logging.getLogger("rabble.transcripts")

_buf: list[dict] = []
_buf_bytes = 0
_lock = threading.RLock()          # guards _buf/_inflight only; never held across network I/O
_flush_lock = threading.Lock()     # serialises flushers
_inflight: list[dict] = []         # swapped out of _buf, PUT in progress
_task: asyncio.Task | None = None


def _data_dir() -> str:
    return os.getenv("DATA_DIR", "/tmp/rabble-data")


def _backend() -> str:
    b = os.getenv("TRANSCRIPT_BACKEND", "auto").strip().lower()
    if b == "auto" or b == "":
        return "r2" if storage.configured() else "file"
    return b


def _flush_s() -> float:
    try:
        return max(1.0, float(os.getenv("TRANSCRIPT_FLUSH_S", "60")))
    except ValueError:
        return 60.0


def _cap() -> tuple[int, int]:
    return (int(os.getenv("TRANSCRIPT_BUFFER_MAX", "500")),
            int(os.getenv("TRANSCRIPT_BUFFER_BYTES", str(4 * 1024 * 1024))))


def pending() -> int:
    return len(_buf)


async def record(
    messages:      list[dict],
    tier:          str,
    workflow_type: str,
) -> None:
    """Persist a completed conversation transcript. Silent on failure — never disrupts the response."""
    ts = datetime.now(timezone.utc).isoformat()
    entry = {
        "ts":            ts,
        "session_id":    str(uuid.uuid4()),
        "tier":          tier,
        "workflow_type": workflow_type,
        "messages":      messages,
    }
    if _backend() != "r2":
        _write_file([entry])
        return
    global _buf_bytes
    size = len(json.dumps(entry, ensure_ascii=False))
    with _lock:
        _buf.append(entry)
        _buf_bytes += size
        over = len(_buf) >= _cap()[0] or _buf_bytes >= _cap()[1]
    if over:
        await asyncio.to_thread(flush_sync, True)
    else:
        _ensure_timer()


def _ensure_timer() -> None:
    global _task
    loop = asyncio.get_running_loop()
    if _task is None or _task.done() or _task.get_loop() is not loop:
        _task = loop.create_task(_timer())


async def _timer() -> None:
    while True:
        await asyncio.sleep(_flush_s())
        await asyncio.to_thread(flush_sync)
        if not _buf:
            return          # idle: the next record() restarts the timer


def flush_sync(spill_overflow: bool = False) -> int:
    """Write the buffer out. Returns entries still buffered. spill_overflow: when the PUT cannot
    happen and the buffer is over its cap, move it to the local file rather than grow.
    The buffer is swapped out under _lock and the PUTs run OUTSIDE it, so record() (on the event
    loop) never waits on the network; what was not written is merged back in front, in order."""
    global _buf_bytes
    with _flush_lock:                      # one flusher at a time; record() never takes this
        with _lock:
            if not _buf:
                return 0
            batch = list(_buf)
            _buf.clear()
            _buf_bytes = 0
            _inflight.extend(batch)
        done: list[dict] = []
        reason = ""
        try:
            by_date: dict[str, list[dict]] = {}
            for e in batch:
                by_date.setdefault(e["ts"][:10], []).append(e)
            for date, entries in by_date.items():
                if storage.exhausted("class_a"):
                    reason = "R2 class A budget spent"
                    break
                key = f"transcripts/{date}/{entries[0]['ts'][11:19].replace(':', '')}-{uuid.uuid4().hex[:8]}.jsonl"
                data = "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in entries).encode()
                try:
                    storage.put(key, data, "application/x-ndjson")
                    done += entries
                except storage.Unavailable as exc:
                    reason = f"R2 unavailable: {exc}"
                    break
                except Exception as exc:
                    reason = f"R2 write failed: {exc}"
                    break
        except BaseException as exc:       # anything unexpected: the entries go back, never vanish
            reason = f"flush failed: {exc!r}"
            logger.error("transcripts: %s", reason)
            raise
        finally:
            with _lock:
                ids = {id(e) for e in done}
                inflight_ids = {id(e) for e in batch}
                _inflight[:] = [e for e in _inflight if id(e) not in inflight_ids]
                rest = [e for e in batch if id(e) not in ids]
                _buf[:] = rest + _buf      # older entries first
                if done:
                    logger.debug("transcripts: %d entries flushed to R2", len(done))
                if reason and _buf:
                    over = len(_buf) >= _cap()[0] or sum(len(json.dumps(e, ensure_ascii=False)) for e in _buf) >= _cap()[1]
                    budget = "budget spent" in reason or "not configured" in reason
                    if budget or (spill_overflow and over):
                        logger.error("transcripts: %s, spilling %d entries to the local file", reason, len(_buf))
                        _write_file(list(_buf))
                        _buf.clear()
                    else:
                        logger.error("transcripts: %s, keeping %d entries buffered for the next flush", reason, len(_buf))
                _buf_bytes = sum(len(json.dumps(e, ensure_ascii=False)) for e in _buf)
                left = len(_buf)
        return left


def _stop_timeout_s() -> float:
    try:
        return max(0.1, float(os.getenv("TRANSCRIPT_STOP_TIMEOUT_S", "10")))
    except ValueError:
        return 10.0


async def stop() -> None:
    """Lifespan stop: flush now (bounded by TRANSCRIPT_STOP_TIMEOUT_S, so a hung R2 PUT cannot eat
    the SIGTERM grace before the db flush); whatever R2 will not take goes to the local file."""
    global _task
    if _task is not None and not _task.done():
        _task.cancel()
    _task = None
    try:
        if _buf or _inflight:   # a timer flush in flight: flush_sync waits on _flush_lock, no duplicate spill
            try:
                await asyncio.wait_for(asyncio.to_thread(flush_sync), timeout=_stop_timeout_s())
            except asyncio.TimeoutError:
                logger.error("transcripts: shutdown flush still running after %.1fs (R2 hanging)",
                             _stop_timeout_s())
    finally:   # whatever the flush did (raised, cancelled, timed out), nothing buffered may die unspilled
        with _lock:
            pending_entries = _inflight + _buf    # in-flight ones included: a hung PUT may never land
            if pending_entries:
                logger.error("transcripts: shutdown with R2 unreachable, spilling %d entries to the local file",
                             len(pending_entries))
                _write_file(list(pending_entries))
                _buf.clear()
                _inflight.clear()


def _write_file(entries: list[dict]) -> None:
    for e in entries:
        path = os.path.join(_data_dir(), "transcripts", f"{e['ts'][:10]}.jsonl")
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "a") as f:
                f.write(json.dumps(e, ensure_ascii=False) + "\n")
        except OSError as exc:
            logger.error("transcript file write failed, ENTRY LOST %s: %s", e.get("session_id"), exc)
