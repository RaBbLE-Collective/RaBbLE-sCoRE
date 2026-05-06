import uuid
import time
import json
import re
import logging
from fastapi import HTTPException

logger = logging.getLogger("rabble.actions")

_pending: dict[str, dict] = {}

_ACTION_TAG_RE = re.compile(r"<action>\s*(\{.*?\})\s*</action>", re.DOTALL | re.IGNORECASE)


def _cleanup_expired() -> None:
    now = time.time()
    expired = [k for k, v in _pending.items() if v["expires_at"] < now]
    for k in expired:
        _pending.pop(k, None)
    if expired:
        logger.info("actions: purged %d expired pending actions", len(expired))


def store(user_id: str, action_type: str, payload: dict) -> str:
    _cleanup_expired()
    action_id = str(uuid.uuid4())
    _pending[action_id] = {
        "type":       action_type,
        "payload":    payload,
        "user_id":    user_id,
        "expires_at": time.time() + 600,
    }
    logger.info("actions: stored action_id=%s type=%s user=%s", action_id, action_type, user_id)
    return action_id


def consume(action_id: str, user_id: str) -> dict:
    action = _pending.pop(action_id, None)
    if not action:
        raise HTTPException(404, "Action not found or already used")
    if action["user_id"] != user_id:
        _pending[action_id] = action  # put it back — wrong user, not consumed
        raise HTTPException(403, "Unauthorized")
    if time.time() > action["expires_at"]:
        raise HTTPException(410, "Action expired — please try again")
    logger.info("actions: consumed action_id=%s type=%s user=%s", action_id, action["type"], user_id)
    return action


def cancel(action_id: str, user_id: str) -> None:
    action = _pending.get(action_id)
    if action and action["user_id"] == user_id:
        _pending.pop(action_id, None)
        logger.info("actions: cancelled action_id=%s user=%s", action_id, user_id)


def extract_action(text: str) -> dict | None:
    """Extract a structured action from <action>…</action> tags.
    Fallback for LLM models that don't support native tool calling."""
    match = _ACTION_TAG_RE.search(text)
    if not match:
        return None
    try:
        return json.loads(match.group(1))
    except json.JSONDecodeError:
        logger.warning("actions: malformed <action> tag JSON: %s", match.group(1)[:200])
        return None
