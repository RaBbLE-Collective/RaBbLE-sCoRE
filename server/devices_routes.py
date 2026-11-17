"""/api/v1/devices: a signed-in member mints, lists and revokes device keys for their Pockets. Rules: devices.py."""

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

import audit
import auth
import devices
import rate_limit
from auth_routes import _require_durable

logger = logging.getLogger("rabble.devices")
router = APIRouter(prefix="/api/v1/devices")


class DeviceRequest(BaseModel):
    label: str = Field(min_length=1, max_length=40)


async def _owner(user: dict = Depends(auth.require_member)) -> dict:
    if user.get("device_id") or user.get("sub") == "pocket":
        raise HTTPException(403, "A device key cannot manage devices; sign in as the member.")
    return user


@router.post("", status_code=201)
async def create_device(body: DeviceRequest, user: dict = Depends(_owner)):
    _require_durable()
    uid = user["sub"]
    label = body.label.strip()
    if not label:
        raise HTTPException(422, "label must not be blank")
    rate_limit.apply_limit(f"device-mint:{uid}", 3600, 10, "device mint")
    made = devices.create(uid, label)
    if not made:
        raise HTTPException(409, f"Device limit reached ({devices.limit()} active). Revoke one to pair another.")
    audit.log(uid, "device.create", 201, tier=user.get("tier"), resource_id=made["id"])
    return {"id": made["id"], "label": made["label"], "key": made["key"], "created_at": made["created_at"]}


@router.get("")
async def list_devices(user: dict = Depends(_owner)):
    return {"devices": devices.list_for(user["sub"]),
            "limit": None if devices.is_exempt(user["sub"]) else devices.limit()}   # null = no limit (admin)


@router.delete("/{device_id}")
async def revoke_device(device_id: str, user: dict = Depends(_owner)):
    _require_durable()
    if not devices.revoke(user["sub"], device_id):
        raise HTTPException(404, "No such device")
    audit.log(user["sub"], "device.revoke", 200, tier=user.get("tier"), resource_id=device_id)
    return {"id": device_id, "revoked": True}
