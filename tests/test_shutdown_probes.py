import asyncio, json, threading, time
import pytest
import db, snapshot, storage, users, transcripts
from test_snapshot_pass4 import U, fresh, _forget, P, POINTER  # noqa


def _reset():
    transcripts._buf.clear(); transcripts._inflight.clear(); transcripts._task = None
    transcripts._buf_bytes = 0


@pytest.fixture(autouse=True)
def _clean_transcript_state():
    """Module state is reset before and after, so a failing test cannot leak entries into the next."""
    _reset()
    yield
    _reset()


def _filed():
    d = db.db_path().parent / "transcripts"
    return [l for f in d.glob("*.jsonl") for l in f.read_text().splitlines()] if d.exists() else []


def _r2_entries(r2):
    out = []
    for k, v in r2.objects.items():
        if "transcripts/" in k:
            out += v.decode().splitlines()
    return out


def test_stop_during_timer_flush_duplicates(data_dir, r2):
    """Shutdown during a timer flush waits for it: the entry lands in R2 once, never also in the spill file."""
    _reset()
    orig = r2.put
    def slow(*a, **k):
        time.sleep(0.5); return orig(*a, **k)
    r2.put = slow
    async def go():
        await transcripts.record([{"role": "user", "content": "dup?"}], "fast", "d")
        fl = asyncio.create_task(asyncio.to_thread(transcripts.flush_sync))
        await asyncio.sleep(0.1)
        await transcripts.stop()
        await fl
    asyncio.run(go())
    f, r = len(_filed()), len(_r2_entries(r2))
    print("file:", f, "r2:", r)
    assert not (f and r), "entry written both to R2 and the spill file (healthy R2, no hang)"


def test_stop_when_flush_raises_loses_entries(data_dir, r2, monkeypatch):
    """storage.exhausted (a db read) raising during the shutdown flush must still spill the entries."""
    _reset()
    async def go():
        await transcripts.record([{"role": "user", "content": "lost?"}], "fast", "d")
        monkeypatch.setattr(storage, "exhausted", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db is locked")))
        try:
            await transcripts.stop()
        except Exception as e:
            print("stop raised:", e)
    asyncio.run(go())
    print("buf:", len(transcripts._buf), "file:", len(_filed()), "r2:", len(_r2_entries(r2)))
    assert _filed() or _r2_entries(r2), "entry neither in R2 nor in the spill file"


def test_stop_cancelled_skips_spill_and_snapshot(data_dir, r2, monkeypatch):
    """A cancelled shutdown (CancelledError is not Exception) must still spill and still run snapshot.stop."""
    import main
    _reset()
    release = threading.Event()
    orig = r2.put
    r2.put = lambda *a, **k: (release.wait(3), orig(*a, **k))[1]
    called = []
    async def snap_stop(): called.append("snap")
    async def snap_start(): pass
    monkeypatch.setattr(snapshot, "stop", snap_stop)
    monkeypatch.setattr(snapshot, "start", snap_start)
    async def go():
        await transcripts.record([{"role": "user", "content": "c"}], "fast", "d")
        cm = main._lifespan(main.app)
        await cm.__aenter__()
        t = asyncio.create_task(cm.__aexit__(None, None, None))
        await asyncio.sleep(0.2); t.cancel()
        try:
            await t
        except asyncio.CancelledError:
            print("lifespan exit cancelled")
    asyncio.run(go())
    release.set(); time.sleep(0.1)
    print("snap stop:", called, "file:", len(_filed()))
    assert called and _filed()


def test_hold_then_pointer_missing_uploads(fresh, monkeypatch):
    """Held boot, pointer later readable: no stale upload, merge, then idempotent (no loop)."""
    snapshot.try_restore()
    users.create_user(U("a")); assert snapshot.flush()
    stale = db.db_path().read_bytes()
    users.create_user(U("late")); assert snapshot.flush()
    ptr_before = fresh.objects[POINTER]
    db.close(); db.db_path().write_bytes(stale); _forget()
    real_get = storage.get
    n = {"c": 0}
    def flaky(key):
        if key == "snapshots/latest.json":
            n["c"] += 1
            if n["c"] in (1, 2):
                raise RuntimeError("transient")
        return real_get(key)
    monkeypatch.setattr(storage, "get", flaky)
    assert snapshot.try_restore() == "present"
    users.create_user(U("x"))
    assert snapshot.flush() is False and snapshot.state == "present" and snapshot.hold   # still unreadable
    assert snapshot.flush() is False and snapshot.state == "failed"
    assert fresh.objects[POINTER] == ptr_before
    assert snapshot.try_restore() == "restored" and snapshot.flush()
    assert snapshot.flush() is False                # idempotent: no loop
    assert users.get_user("late") and users.get_user("x")
