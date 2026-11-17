"""/api/v1/pocket: a signed-in member reads the captures and conversations their Pockets kept. Storage: pocket_store.py."""

from fastapi import APIRouter, Depends, HTTPException, Query, Request

import auth
import pocket_store as store

router = APIRouter(prefix="/api/v1/pocket")


async def _member(request: Request, user: dict = Depends(auth.require_user)) -> dict:
    """Admin and members only: guests, entity accounts and the shared keys get 401 (their data is not kept)."""
    if not store.persists(user):
        raise HTTPException(401, "Sign in as a member to read Pocket data.")
    return user


def _before(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return store.norm_ts(value)
    except ValueError:
        raise HTTPException(422, "before must be an ISO timestamp")


@router.get("/notes")
async def list_notes(limit: int = Query(50, ge=1, le=200), before: str | None = None, user: dict = Depends(_member)):
    return {"notes": store.list_notes(user["sub"], limit, _before(before))}


@router.get("/notes/{note_id}")
async def get_note(note_id: str, user: dict = Depends(_member)):
    note = store.get_note(user["sub"], note_id)
    if not note:
        raise HTTPException(404, "No such note")
    return note


@router.delete("/notes/{note_id}")
async def delete_note(note_id: str, user: dict = Depends(_member)):
    if not store.delete_note(user["sub"], note_id):
        raise HTTPException(404, "No such note")
    return {"ok": True}


@router.get("/conversations")
async def list_conversations(limit: int = Query(30, ge=1, le=200), before: str | None = None,
                             user: dict = Depends(_member)):
    return {"conversations": store.list_conversations(user["sub"], limit, _before(before))}


@router.get("/conversations/{cid}")
async def get_conversation(cid: str, user: dict = Depends(_member)):
    convo = store.get_conversation(user["sub"], cid)
    if not convo:
        raise HTTPException(404, "No such conversation")
    return convo


@router.delete("/conversations/{cid}")
async def delete_conversation(cid: str, user: dict = Depends(_member)):
    if not store.delete_conversation(user["sub"], cid):
        raise HTTPException(404, "No such conversation")
    return {"ok": True}
