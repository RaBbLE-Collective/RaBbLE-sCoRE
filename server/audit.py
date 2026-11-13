import json
import os
import logging
from datetime import datetime, timezone

logger = logging.getLogger("rabble.audit")

LOG_PATH = os.getenv("AUDIT_LOG_PATH", "audit.jsonl")


def log(
    user_id:       str,
    action:        str,
    status:        int,
    tier:          str | None = None,
    workflow_type: str | None = None,
    resource_id:   str | None = None,
    error:         str | None = None,
) -> None:
    entry: dict = {
        "ts":      datetime.now(timezone.utc).isoformat(),
        "user_id": user_id,
        "action":  action,
        "status":  status,
    }
    if tier:          entry["tier"]          = tier
    if workflow_type: entry["workflow_type"] = workflow_type
    if resource_id:   entry["resource_id"]   = resource_id
    if error:         entry["error"]          = error

    try:
        with open(LOG_PATH, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError as exc:
        logger.error("audit log write failed: %s entry=%s", exc, entry)
