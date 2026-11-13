"""Capture turns on the beam (server/capture.py, server/beam.py). No network:
the LLM call is monkeypatched. Run: server/.venv/bin/python -m pytest tests"""

import json
import os
import sys
from datetime import timezone

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))

os.environ.setdefault("JWT_SECRET", "test-secret-not-real-0123456789")

import audit  # noqa: E402
import beam  # noqa: E402
import capture  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

STARTED = 1792639812   # 2026-10-22 03:30:12 UTC
SHAPED = {"title": "Backplate ideas", "tags": ["pocket", "cad"],
          "summary": ["Tolerance is tight"], "notes": "Tolerance is **tight**."}


def test_created_uses_tz():
    local = capture.local_time(STARTED, "America/New_York")
    assert local.isoformat() == "2026-10-21T23:30:12-04:00"


def test_created_unknown_or_missing_tz_is_utc():
    assert capture.local_time(STARTED, "Nowhere/Land").utcoffset().total_seconds() == 0
    assert capture.local_time(STARTED, None).isoformat() == "2026-10-22T03:30:12+00:00"


def test_created_missing_started_is_now():
    assert capture.local_time(None, None).tzinfo is not None
    assert capture.local_time("junk", "UTC").year >= 2026


def test_safe_id():
    assert capture.safe_id("20261022-0130-5eb8-7", STARTED, None) == "20261022-0130-5eb8-7"
    for bad in (None, "", "../etc/passwd", "UPPER", "a" * 65, "has space", 12):
        assert capture.safe_id(bad, STARTED, "UTC", 3) == "20261022-0330-x-3"


def test_assemble_shaped():
    created = capture.local_time(STARTED, "America/New_York")
    md = capture.assemble_note("n-1", created, "RaBbLE-Pocket-5EB8", "voice", 94,
                               "um so the tolerance is tight", SHAPED)
    head, body = md.split("\n---\n", 1)
    for line in ("id: n-1", "type: pocket-note", 'title: "Backplate ideas"',
                 "created: 2026-10-21T23:30:12-04:00", 'device: "RaBbLE-Pocket-5EB8"',
                 "source: voice", "duration_s: 94", "tags: [pocket, cad]"):
        assert line in head
    assert "## Summary\n- Tolerance is tight" in body
    assert "## Notes\nTolerance is **tight**." in body
    assert body.rstrip().endswith("## Transcript\num so the tolerance is tight")


def test_assemble_text_has_no_duration():
    md = capture.assemble_note("n", capture.local_time(STARTED, "UTC"), "d", "text", None, "hi", SHAPED)
    assert "duration_s" not in md and "source: text" in md


def test_assemble_fallback():
    words = "word " * 40
    md = capture.assemble_note("n", capture.local_time(STARTED, "UTC"), "d", "voice", 5, words.strip(), None)
    title = capture.note_title(md)
    assert 0 < len(title) <= 60 and title.startswith("word word")
    assert "tags: []" in md and "## Summary" not in md and "## Notes" not in md
    assert md.rstrip().endswith("## Transcript\n" + words.strip())


def test_transcript_verbatim_even_with_odd_text():
    t = "line one\n\n## Notes\n- not a heading, éü 日本語 ---"
    md = capture.assemble_note("n", capture.local_time(STARTED, "UTC"), "d", "text", None, t, SHAPED)
    assert md.endswith("## Transcript\n" + t + "\n")


def test_parse_shaped():
    raw = '```json\n{"title":"  A very long title ' + "x" * 80 + '","tags":["Foo Bar","foo-bar","a","b","c","d","e"],' \
          '"summary":["- one","two"],"notes":"n"}\n```'
    got = capture.parse_shaped(raw)
    assert len(got["title"]) <= 60
    assert got["tags"] == ["foo-bar", "a", "b", "c", "d"]
    assert got["summary"] == ["one", "two"]
    assert capture.parse_shaped("Sure! " + json.dumps(SHAPED) + " hope that helps")["title"] == "Backplate ideas"
    for bad in ("", "not json", "[1]", '{"title": ""}', "{broken"):
        assert capture.parse_shaped(bad) is None


def test_prompt_forbids_inventing():
    system, messages = capture.build_prompt("hello there", "text")
    assert "Never add facts" in system and "hello there" in messages[0]["content"]


@pytest.mark.parametrize("text", ["a" * 20000, "日本語é" * 4000, "😀" * 5000, "x" + "é" * 9000])
def test_split_parts(text):
    parts = capture.split_parts(text)
    assert "".join(parts) == text
    assert all(len(p.encode()) <= 8192 for p in parts)
    assert len(parts) == len(text.encode()) // 8192 + (1 if len(text.encode()) % 8192 else 0) or len(parts) >= 2


def test_split_parts_small_and_empty():
    assert capture.split_parts("abc") == ["abc"]
    assert capture.split_parts("") == [""]


# ── over the WebSocket ───────────────────────────────────────────────────────

@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("BEAM_ALLOW_GUEST", "true")
    monkeypatch.setattr(audit, "LOG_PATH", str(tmp_path / "audit.jsonl"))
    beam._kept.clear()
    beam._history.clear()
    calls = []

    async def fake_stream(messages, system, **kw):
        calls.append(messages)
        yield json.dumps({"title": "Backplate ideas", "tags": ["pocket"], "summary": ["tight"],
                          "notes": "Tolerance is tight."})
    monkeypatch.setattr(beam.llm, "stream_chat", fake_stream)
    app = FastAPI()
    app.include_router(beam.router)
    c = TestClient(app)
    c.llm_calls = c.calls = calls
    c.audit_path = tmp_path / "audit.jsonl"
    return c


def _hello(ws):
    ws.send_text(json.dumps({"type": "hello", "device": "RaBbLE-Pocket-5EB8", "proto": "rabble-beam/0"}))
    return json.loads(ws.receive_text())


def _note_from(ws, first_turn=None):
    parts = []
    while True:
        msg = json.loads(ws.receive_text())
        assert msg["type"] == "note", msg
        parts.append(msg)
        if msg["last"]:
            return parts


def test_typed_capture_roundtrip(client):
    with client.websocket_connect("/api/v1/beam") as ws:
        assert _hello(ws)["type"] == "ready"
        ws.send_text(json.dumps({"type": "turn.begin", "turn": 4, "mode": "capture", "id": "20261022-0330-5eb8-4",
                                 "started": STARTED, "tz": "America/New_York", "text": "Backplate tolerance is tight"}))
        assert json.loads(ws.receive_text()) == {"type": "sent", "turn": 4}
        parts = _note_from(ws)
        assert json.loads(ws.receive_text()) == {"type": "done", "turn": 4}
    assert [p["seq"] for p in parts] == list(range(len(parts)))
    assert parts[0]["title"] == "Backplate ideas"
    assert parts[0]["path"] == "2026/10/20261022-0330-5eb8-4.md"
    assert parts[0]["turn"] == 4
    md = "".join(p["md"] for p in parts)
    assert md.startswith("---\nid: 20261022-0330-5eb8-4\n")
    assert "created: 2026-10-21T23:30:12-04:00" in md
    assert "source: text" in md and "## Summary\n- tight" in md
    assert md.endswith("## Transcript\nBackplate tolerance is tight\n")
    assert beam._history == {}                      # never touches the conversation
    assert '"workflow_type": "capture"' in client.audit_path.read_text()


def test_unsaved_note_is_resent_saved_one_is_not(client):
    with client.websocket_connect("/api/v1/beam") as ws:
        _hello(ws)
        ws.send_text(json.dumps({"type": "turn.begin", "turn": 1, "mode": "capture", "id": "n-1", "text": "one"}))
        for _ in range(3):
            ws.receive_text()                         # sent, note, done
        ws.send_text(json.dumps({"type": "turn.begin", "turn": 2, "mode": "capture", "id": "n-2", "text": "two"}))
        for _ in range(3):
            ws.receive_text()
        ws.send_text(json.dumps({"type": "note.saved", "id": "n-1"}))
    with client.websocket_connect("/api/v1/beam") as ws:
        assert _hello(ws)["type"] == "ready"
        parts = _note_from(ws)
        assert parts[0]["turn"] == 0 and parts[0]["id"] == "n-2"
        ws.send_text(json.dumps({"type": "note.saved", "id": "n-2"}))
    with client.websocket_connect("/api/v1/beam") as ws:
        _hello(ws)
        ws.send_text(json.dumps({"type": "turn.begin", "turn": 3, "mode": "capture", "id": "n-3", "text": "three"}))
        assert json.loads(ws.receive_text())["type"] == "sent"   # nothing resent before it
        assert json.loads(ws.receive_text())["id"] == "n-3"


def test_shaping_failure_still_sends_note(client, monkeypatch):
    async def boom(*a, **k):
        raise RuntimeError("llm down")
        yield
    monkeypatch.setattr(beam.llm, "stream_chat", boom)
    with client.websocket_connect("/api/v1/beam") as ws:
        _hello(ws)
        ws.send_text(json.dumps({"type": "turn.begin", "turn": 1, "mode": "capture", "text": "just some words here"}))
        ws.receive_text()
        parts = _note_from(ws)
        assert json.loads(ws.receive_text())["type"] == "done"
    md = "".join(p["md"] for p in parts)
    assert parts[0]["title"] == "just some words here"
    assert "## Summary" not in md and md.endswith("## Transcript\njust some words here\n")
    assert parts[0]["id"].endswith("-x-1")           # no id from the device: made here


def test_empty_typed_capture_errors(client):
    with client.websocket_connect("/api/v1/beam") as ws:
        _hello(ws)
        ws.send_text(json.dumps({"type": "turn.begin", "turn": 1, "mode": "capture", "text": "  "}))
        ws.receive_text()
        assert json.loads(ws.receive_text())["text"] == "DIDN'T CATCH THAT"


def test_capture_audio_cap_exceeds_voice_cap(monkeypatch):
    class S(beam.BeamSession):
        def __init__(self):
            self.turn, self.pcm, self.turn_mode = 1, bytearray(), "capture"
    s = S()
    frame = bytes([1, 0, 1, 0]) + b"\0" * 3200
    for _ in range(beam.MAX_NOTE_BYTES // 3200 + 20):
        s.on_audio(frame)
    assert len(s.pcm) > beam.MAX_NOTE_BYTES
    s.turn_mode, s.pcm = "voice", bytearray()
    for _ in range(beam.MAX_NOTE_BYTES // 3200 + 20):
        s.on_audio(frame)
    assert beam.MAX_NOTE_BYTES <= len(s.pcm) < beam.MAX_NOTE_BYTES + 3200
