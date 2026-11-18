"""Pass 5: a held boot still gets the lineage check; lifespan always stops the snapshot; transcript PUTs never hold the lock."""

import asyncio
import json
import sqlite3
import threading
import time

import pytest

import db
import snapshot
import storage
import transcripts
import users
from test_snapshot_pass4 import U, fresh, _forget, P, POINTER  # noqa: F401


def test_hold_path_runs_the_lineage_check(fresh, monkeypatch):
    snapshot.try_restore()
    users.create_user(U("a"))
    assert snapshot.flush()
    stale = db.db_path().read_bytes()
    users.create_user(U("late"))
    assert snapshot.flush()
    ptr_before = fresh.objects[POINTER]
    db.close(); db.db_path().write_bytes(stale)
    _forget()
    real_get, calls = storage.get, {"n": 0}

    def flaky(key):
        calls["n"] += 1
        if key == "snapshots/latest.json" and calls["n"] == 1:
            raise RuntimeError("transient")
        return real_get(key)
    monkeypatch.setattr(storage, "get", flaky)
    assert snapshot.try_restore() == "present" and snapshot.hold
    users.create_user(U("x"))
    assert snapshot.flush() is False                   # diverged: never uploaded
    assert fresh.objects[POINTER] == ptr_before and snapshot.state == "failed"
    assert snapshot.try_restore() == "restored"        # merge
    assert {users.get_user(i) is not None for i in ("a", "late", "x")} == {True}
    assert snapshot.flush()
    head = fresh.objects[P + json.loads(fresh.objects[POINTER])["key"]]
    c = sqlite3.connect(":memory:"); c.deserialize(head)
    assert {r[0] for r in c.execute("SELECT id FROM users")} >= {"a", "late", "x"}


def _lifespan_run(monkeypatch, transcripts_stop):
    import main
    called = []

    async def snap_stop(): called.append("snap")
    async def snap_start(): pass
    monkeypatch.setattr(transcripts, "stop", transcripts_stop)
    monkeypatch.setattr(snapshot, "stop", snap_stop)
    monkeypatch.setattr(snapshot, "start", snap_start)

    async def go():
        cm = main._lifespan(main.app)
        await cm.__aenter__()
        await cm.__aexit__(None, None, None)
    asyncio.run(go())
    return called


def test_lifespan_stops_snapshot_even_if_transcripts_stop_raises(data_dir, monkeypatch, caplog):
    async def boom(): raise RuntimeError("transcripts stop failed")
    with caplog.at_level("ERROR"):
        assert _lifespan_run(monkeypatch, boom) == ["snap"]
    assert "transcripts.stop() failed" in caplog.text


def test_stop_is_bounded_and_spills_when_the_put_hangs(r2, monkeypatch, caplog):
    monkeypatch.setenv("TRANSCRIPT_STOP_TIMEOUT_S", "0.3")
    transcripts._buf.clear(); transcripts._inflight.clear(); transcripts._task = None
    release = threading.Event()
    orig = r2.put

    def hang(*a, **k):
        release.wait(5)
        return orig(*a, **k)
    r2.put = hang

    async def go():
        await transcripts.record([{"role": "user", "content": "keep me"}], "fast", "d")
        t0 = time.monotonic()
        with caplog.at_level("ERROR"):
            await transcripts.stop()
        return time.monotonic() - t0
    took = asyncio.run(go())
    release.set()
    assert took < 2
    assert "still running" in caplog.text and "spilling 1 entries" in caplog.text
    files = list((db.db_path().parent / "transcripts").glob("*.jsonl"))
    assert files and "keep me" in files[0].read_text()
    time.sleep(0.2)
    transcripts._buf.clear(); transcripts._inflight.clear()


def test_record_stays_fast_during_a_slow_put(data_dir, r2):
    transcripts._buf.clear(); transcripts._inflight.clear(); transcripts._task = None
    orig = r2.put

    def slow(*a, **k):
        time.sleep(1.0); return orig(*a, **k)
    r2.put = slow

    async def go():
        await transcripts.record([{"role": "user", "content": "a"}], "fast", "d")
        flusher = asyncio.create_task(asyncio.to_thread(transcripts.flush_sync))
        await asyncio.sleep(0.1)
        t0 = time.monotonic()
        await transcripts.record([{"role": "user", "content": "b"}], "fast", "d")
        blocked = time.monotonic() - t0
        await flusher
        return blocked
    blocked = asyncio.run(go())
    assert blocked < 0.3
    assert transcripts.pending() == 1                  # b stayed buffered, a went up
    transcripts._buf.clear()


def test_failed_put_merges_entries_back_in_order(data_dir, r2):
    transcripts._buf.clear(); transcripts._inflight.clear()
    mk = lambda n: {"ts": f"2026-11-15T00:00:0{n}+00:00", "session_id": str(n), "messages": []}
    transcripts._buf.extend([mk(1), mk(2)])
    orig = r2.put

    def put(*a, **k):
        transcripts._buf.append(mk(3))                  # a record() lands mid-PUT
        raise RuntimeError("R2 down")
    r2.put = put
    assert transcripts.flush_sync() == 3
    assert [e["session_id"] for e in transcripts._buf] == ["1", "2", "3"]
    r2.put = orig
    assert transcripts.flush_sync() == 0
    transcripts._buf.clear()
