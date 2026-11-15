"""
Transcript persistence — RaBbLE training data capture. /chat and session turns only.

Backends (set via TRANSCRIPT_BACKEND env var):
  r2   — object per conversation, transcripts/YYYY-MM-DD/<session>.json, through
          storage.py (R2_* env; counted against the monthly budget)
  file — DATA_DIR/transcripts/YYYY-MM-DD.jsonl (default; ephemeral on Render free tier)

Fallback: if R2 is unset, over budget or the write fails, the entry goes to the file.

Privacy contract:
  - session_id is a random UUID generated per conversation — never the user_id
  - No identity fields (user_id, IP, handle) are stored
  - Only message content, tier, workflow type, and timestamp are captured
"""

import asyncio
import json
import logging
import os
import uuid
from datetime import datetime, timezone

import storage

logger = logging.getLogger("rabble.transcripts")


def _data_dir() -> str:
    return os.getenv("DATA_DIR", "/tmp/rabble-data")


async def record(
    messages:      list[dict],
    tier:          str,
    workflow_type: str,
) -> None:
    """Persist a completed conversation transcript. Silent on failure — never disrupts the response."""
    session_id = str(uuid.uuid4())
    ts = datetime.now(timezone.utc).isoformat()
    entry = {
        "ts":            ts,
        "session_id":    session_id,
        "tier":          tier,
        "workflow_type": workflow_type,
        "messages":      messages,
    }

    if os.getenv("TRANSCRIPT_BACKEND", "file") == "r2":
        await _write_r2(session_id, ts, entry)
    else:
        _write_file(ts, entry)


async def _write_r2(session_id: str, ts: str, entry: dict) -> None:
    date = ts[:10]  # YYYY-MM-DD
    key  = f"transcripts/{date}/{session_id}.json"
    try:
        await asyncio.to_thread(storage.put, key, json.dumps(entry, ensure_ascii=False).encode(),
                                "application/json")
        logger.debug("transcript saved to R2: %s/%s", date, session_id)
    except storage.Unavailable as exc:
        logger.debug("transcript to file, R2 unavailable: %s", exc)
        _write_file(ts, entry)
    except Exception as exc:
        logger.error("transcript R2 write failed, falling back to file: %s", exc)
        _write_file(ts, entry)


def _write_file(ts: str, entry: dict) -> None:
    date = ts[:10]
    path = os.path.join(_data_dir(), "transcripts", f"{date}.jsonl")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    try:
        with open(path, "a") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        logger.debug("transcript saved to file: %s", path)
    except OSError as exc:
        logger.error("transcript file write failed: %s", exc)
