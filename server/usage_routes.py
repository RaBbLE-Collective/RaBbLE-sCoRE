"""
Usage totals, limits and the account event log. Metering and rules: usage.py; events: account_events.py.

  GET  /api/v1/usage?days=30                      the caller's own totals, limits and what is left (member)
  GET  /api/v1/admin/usage?days=30                per account totals, busiest first, top users (admin)
  GET  /api/v1/admin/users/{user_id}/usage        one account's totals (admin)
  GET  /api/v1/admin/users/{user_id}/limits       its limits: effective, own overrides, tier defaults (admin)
  PUT  /api/v1/admin/users/{user_id}/limits       set fields (-1 unlimited, null back to the tier default) (admin)
  GET  /api/v1/admin/events?user_id=&limit=       the durable account event log, newest first (admin)

Admin routes authenticate exactly as the other /admin routes (auth_routes._require_admin): the shared admin key or
an admin-tier user; a device key never reaches them.
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

import account_events
import auth
import db
import usage
from auth_routes import _require_admin, _require_durable

router = APIRouter(prefix="/api/v1")

_MAX_LIMIT = 1_000_000_000


class LimitsBody(BaseModel):
    """Fields sent are set; fields left out are unchanged. null = use the tier default, -1 = unlimited."""
    daily_turns: int | None = Field(default=None, ge=-1, le=_MAX_LIMIT)
    monthly_turns: int | None = Field(default=None, ge=-1, le=_MAX_LIMIT)
    daily_audio_s: int | None = Field(default=None, ge=-1, le=_MAX_LIMIT)
    monthly_audio_s: int | None = Field(default=None, ge=-1, le=_MAX_LIMIT)
    note: str | None = Field(default=None, max_length=200)


def _who(admin: dict) -> str:
    return str(admin.get("id") or admin.get("sub") or "admin")


def _target(user_id: str) -> dict:
    row = db.query_one("SELECT id, handle, tier FROM users WHERE id = ?", (user_id,))
    if not row:
        raise HTTPException(404, "No such account")
    return row


@router.get("/usage")
async def my_usage(days: int = Query(30, ge=1, le=366), user: dict = Depends(auth.require_member)):
    return usage.summary(user["sub"], days, tier=usage.tier_of(user))


@router.get("/admin/usage")
async def admin_usage(days: int = Query(30, ge=1, le=366), limit: int = Query(100, ge=1, le=500),
                      admin: dict = Depends(_require_admin)):
    return usage.admin_summary(days, limit)


@router.get("/admin/users/{user_id}/usage")
async def user_usage(user_id: str, days: int = Query(30, ge=1, le=366), admin: dict = Depends(_require_admin)):
    row = _target(user_id)
    return {**usage.summary(user_id, days, tier=row["tier"]), "handle": row["handle"]}


def _limits_view(user_id: str, row: dict) -> dict:
    info = usage.limits_for(user_id, row["tier"])
    own = db.query_one("SELECT * FROM account_limits WHERE user_id = ?", (user_id,)) or {}
    return {"user_id": user_id, "handle": row["handle"], "tier": info["tier"], "limits": info["limits"],
            "source": info["source"], "own": {f: own.get(f) for f in usage.FIELDS}, "defaults": info["defaults"],
            "note": info["note"], "updated_by": info["updated_by"], "updated_at": info["updated_at"]}


@router.get("/admin/users/{user_id}/limits")
async def get_limits(user_id: str, admin: dict = Depends(_require_admin)):
    return _limits_view(user_id, _target(user_id))


@router.put("/admin/users/{user_id}/limits")
async def put_limits(user_id: str, body: LimitsBody, admin: dict = Depends(_require_admin)):
    row = _target(user_id)
    _require_durable()
    sent = body.model_fields_set
    changes = {f: getattr(body, f) for f in usage.FIELDS if f in sent}
    before = usage.limits_for(user_id, row["tier"])["limits"]
    usage.set_limits(user_id, changes, _who(admin), body.note if "note" in sent else None)
    after = usage.limits_for(user_id, row["tier"])["limits"]
    account_events.record(user_id, "limit.set", detail={"by": _who(admin), "set": changes, "before": before,
                                                        "after": after})
    return _limits_view(user_id, row)


@router.get("/admin/events")
async def admin_events(user_id: str | None = None, limit: int = Query(100, ge=1, le=500),
                       admin: dict = Depends(_require_admin)):
    return {"events": account_events.recent(user_id, limit)}
