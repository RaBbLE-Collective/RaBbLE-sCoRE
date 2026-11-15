import json
import os
import logging
from datetime import datetime, timezone

logger = logging.getLogger("rabble.audit")

# Under DATA_DIR unless AUDIT_LOG_PATH says otherwise, so the log lives (and is exported) with the data.
LOG_PATH = os.getenv("AUDIT_LOG_PATH") or os.path.join(os.getenv("DATA_DIR", "data"), "audit.jsonl")


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
        os.makedirs(os.path.dirname(LOG_PATH) or ".", exist_ok=True)
        with open(LOG_PATH, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError as exc:
        logger.error("audit log write failed: %s entry=%s", exc, entry)
