"""Transcripts buffer in memory and flush to R2 in batches, with a file as the last resort."""

import asyncio
import json

import pytest

import storage
import transcripts


@pytest.fixture(autouse=True)
def _clean_buffer():
    transcripts._buf.clear()
    transcripts._buf_bytes = 0
    transcripts._task = None
    yield
    transcripts._buf.clear()


def _rec(n=1):
    async def go():
        for i in range(n):
            await transcripts.record([{"role": "user", "content": f"hi{i}"}], "fast", "default")
    asyncio.run(go())


def _files(data_dir):
    return list((data_dir / "transcripts").glob("*.jsonl"))


def _puts(r2):
    return [c for c in r2.calls if c[0] == "put"]


def _lines(r2):
    return [json.loads(l) for v in r2.objects.values() for l in v.decode().splitlines()]


def test_file_is_the_default_without_r2(data_dir):
    _rec()
    (f,) = _files(data_dir)
    assert json.loads(f.read_text())["messages"][0]["content"] == "hi0"


def test_auto_picks_r2_when_configured(data_dir, r2):
    _rec()
    assert transcripts.pending() == 1 and r2.objects == {} and not _files(data_dir)


def test_n_turns_flush_as_one_put(data_dir, r2):
    _rec(25)
    assert _puts(r2) == []                       # nothing per turn
    assert transcripts.flush_sync() == 0
    assert len(_puts(r2)) == 1
    (key,) = r2.objects
    assert key.startswith("score/transcripts/") and key.endswith(".jsonl")
    got = _lines(r2)
    assert len(got) == 25 and all("user_id" not in e for e in got)
    assert transcripts.flush_sync() == 0 and len(_puts(r2)) == 1     # clean buffer: no request
    assert not _files(data_dir)


def test_shutdown_flushes(data_dir, r2):
    _rec(3)
    asyncio.run(transcripts.stop())
    assert len(_lines(r2)) == 3 and transcripts.pending() == 0 and not _files(data_dir)


def test_failure_retries_without_loss(data_dir, r2):
    _rec(4)
    r2.fail = True
    assert transcripts.flush_sync() == 4         # kept, not dropped, not on disk
    assert not _files(data_dir)
    _rec(1)
    r2.fail = False
    assert transcripts.flush_sync() == 0
    assert len(_lines(r2)) == 5


def test_shutdown_with_r2_down_spills_to_file(data_dir, r2):
    _rec(2)
    r2.fail = True
    asyncio.run(transcripts.stop())
    assert transcripts.pending() == 0
    assert len(_files(data_dir)[0].read_text().splitlines()) == 2


def test_budget_exhaustion_spills_without_r2_io(data_dir, r2, monkeypatch):
    monkeypatch.setenv("R2_CLASS_A_BUDGET", "1")
    storage.put("x", b"1")
    r2.calls.clear()
    _rec(3)
    assert transcripts.flush_sync() == 0
    assert r2.calls == []
    assert len(_files(data_dir)[0].read_text().splitlines()) == 3


def test_buffer_cap_forces_flush_then_spills_if_down(data_dir, r2, monkeypatch):
    monkeypatch.setenv("TRANSCRIPT_BUFFER_MAX", "3")
    r2.fail = True
    _rec(3)                                      # hits the cap while R2 is down
    assert transcripts.pending() == 0
    assert len(_files(data_dir)[0].read_text().splitlines()) == 3


def test_explicit_file_backend_writes_immediately(data_dir, r2, monkeypatch):
    monkeypatch.setenv("TRANSCRIPT_BACKEND", "file")
    _rec()
    assert len(_files(data_dir)) == 1 and r2.objects == {} and transcripts.pending() == 0


def test_timer_flushes(data_dir, r2, monkeypatch):
    monkeypatch.setenv("TRANSCRIPT_FLUSH_S", "1")
    async def go():
        await transcripts.record([{"role": "user", "content": "t"}], "fast", "default")
        await asyncio.sleep(1.6)
    asyncio.run(go())
    assert len(_lines(r2)) == 1
