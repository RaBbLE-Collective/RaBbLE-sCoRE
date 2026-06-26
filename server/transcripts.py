"""
Transcript persistence — RaBbLE training data capture.

Backends (set via TRANSCRIPT_BACKEND env var):
  r2   — Cloudflare R2 via management API (durable; requires CF_ACCOUNT_ID,
          CF_R2_TOKEN with Workers R2 Storage:Edit, CF_R2_BUCKET)
  file — DATA_DIR/transcripts/YYYY-MM-DD.jsonl (default; ephemeral on Render free tier)

R2 fallback: if R2 write fails, the entry falls back to file automatically.

Privacy contract:
  - session_id is a random UUID generated per conversation — never the user_id
  - No identity fields (user_id, IP, handle) are stored
  - Only message content, tier, workflow type, and timestamp are captured
"""

import json
import logging
import os
import uuid
from datetime import datetime, timezone

import httpx

logger = logging.getLogger("rabble.transcripts")

DATA_DIR      = os.getenv("DATA_DIR", "/tmp/rabble-data")
_BACKEND      = os.getenv("TRANSCRIPT_BACKEND", "file")
_CF_ACCOUNT   = os.getenv("CF_ACCOUNT_ID", "")
_CF_R2_TOKEN  = os.getenv("CF_R2_TOKEN", "")
_CF_R2_BUCKET = os.getenv("CF_R2_BUCKET", "")


def _r2_ready() -> bool:
    return bool(_CF_ACCOUNT and _CF_R2_TOKEN and _CF_R2_BUCKET)


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

    if _BACKEND == "r2" and _r2_ready():
        await _write_r2(session_id, ts, entry)
    else:
        _write_file(ts, entry)


async def _write_r2(session_id: str, ts: str, entry: dict) -> None:
    date = ts[:10]  # YYYY-MM-DD
    key  = f"transcripts/{date}/{session_id}.json"
    url  = (
        f"https://api.cloudflare.com/client/v4/accounts/{_CF_ACCOUNT}"
        f"/r2/buckets/{_CF_R2_BUCKET}/objects/{key}"
    )
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.put(
                url,
                content=json.dumps(entry, ensure_ascii=False).encode(),
                headers={
                    "Authorization": f"Bearer {_CF_R2_TOKEN}",
                    "Content-Type":  "application/json",
                },
            )
            if not resp.is_success:
                raise RuntimeError(f"R2 {resp.status_code}: {resp.text[:200]}")
            logger.debug("transcript saved to R2: %s/%s", date, session_id)
    except Exception as exc:
        logger.error("transcript R2 write failed, falling back to file: %s", exc)
        _write_file(ts, entry)


def _write_file(ts: str, entry: dict) -> None:
    date = ts[:10]
    path = os.path.join(DATA_DIR, "transcripts", f"{date}.jsonl")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    try:
        with open(path, "a") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        logger.debug("transcript saved to file: %s", path)
    except OSError as exc:
        logger.error("transcript file write failed: %s", exc)
