"""Transcripts go through storage.py with a file fallback."""

import asyncio
import json

import storage
import transcripts


def _rec():
    asyncio.run(transcripts.record([{"role": "user", "content": "hi"}], "fast", "default"))


def test_file_backend_is_the_default(data_dir, r2):
    _rec()
    files = list((data_dir / "transcripts").glob("*.jsonl"))
    assert len(files) == 1 and json.loads(files[0].read_text())["messages"][0]["content"] == "hi"
    assert r2.objects == {}


def test_r2_backend_writes_one_object_per_conversation(data_dir, r2, monkeypatch):
    monkeypatch.setenv("TRANSCRIPT_BACKEND", "r2")
    _rec()
    (key,) = r2.objects
    parts = key.split("/")
    assert parts[:2] == ["score", "transcripts"] and parts[-1].endswith(".json") and len(parts[2]) == 10
    entry = json.loads(r2.objects[key])
    assert "user_id" not in entry and entry["session_id"] == parts[-1][:-5]
    assert not (data_dir / "transcripts").exists()


def test_r2_failure_or_budget_falls_back_to_file(data_dir, r2, monkeypatch):
    monkeypatch.setenv("TRANSCRIPT_BACKEND", "r2")
    r2.fail = True
    _rec()
    assert len(list((data_dir / "transcripts").glob("*.jsonl"))) == 1
    r2.fail = False
    monkeypatch.setenv("R2_CLASS_A_BUDGET", "2")        # the failed write above already cost one
    storage.put("x", b"1")
    r2.calls.clear()
    _rec()
    assert r2.calls == []                       # no R2 I/O once the budget is spent
    lines = list((data_dir / "transcripts").glob("*.jsonl"))[0].read_text().splitlines()
    assert len(lines) == 2


def test_r2_backend_without_r2_uses_file(data_dir, monkeypatch):
    monkeypatch.setenv("TRANSCRIPT_BACKEND", "r2")
    _rec()
    assert len(list((data_dir / "transcripts").glob("*.jsonl"))) == 1
