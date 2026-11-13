import uuid
import time
import logging
from fastapi import HTTPException

logger = logging.getLogger("rabble.workflows")

_store: dict[str, dict] = {}

WORKFLOW_TTL = 60 * 60 * 4  # 4 hours


def _evict_expired() -> None:
    now = time.time()
    stale = [k for k, v in _store.items() if v["expires_at"] < now]
    for k in stale:
        _store.pop(k, None)
    if stale:
        logger.info("workflows: evicted %d expired records", len(stale))


def create(user_id: str, workflow_type: str, goal: str, model_tier: str = "medium") -> dict:
    _evict_expired()
    wf_id = str(uuid.uuid4())
    record = {
        "id":          wf_id,
        "user_id":     user_id,
        "type":        workflow_type,
        "goal":        goal,
        "model_tier":  model_tier,
        "state":       "pending",
        "steps":       [],
        "messages":    [],
        "result":      None,
        "created_at":  time.time(),
        "updated_at":  time.time(),
        "expires_at":  time.time() + WORKFLOW_TTL,
    }
    _store[wf_id] = record
    logger.info("workflow created wf_id=%s type=%s user=%s", wf_id, workflow_type, user_id)
    return record


def get(wf_id: str, user_id: str) -> dict:
    record = _store.get(wf_id)
    if not record or time.time() > record["expires_at"]:
        _store.pop(wf_id, None)
        raise HTTPException(404, "Workflow not found or expired")
    if record["user_id"] != user_id:
        raise HTTPException(403, "Unauthorized")
    return record


def set_state(wf_id: str, state: str, step: dict | None = None, result: str | None = None) -> dict:
    record = _store.get(wf_id)
    if not record:
        raise HTTPException(404, "Workflow not found")
    record["state"]      = state
    record["updated_at"] = time.time()
    if step is not None:
        record["steps"].append(step)
    if result is not None:
        record["result"] = result
    return record


def add_message(wf_id: str, role: str, content: str) -> None:
    record = _store.get(wf_id)
    if record:
        record["messages"].append({"role": role, "content": content})
        record["updated_at"] = time.time()


def cancel(wf_id: str, user_id: str) -> None:
    record = get(wf_id, user_id)
    if record["state"] not in ("completed", "failed", "cancelled"):
        record["state"]      = "cancelled"
        record["updated_at"] = time.time()
        logger.info("workflow cancelled wf_id=%s user=%s", wf_id, user_id)
