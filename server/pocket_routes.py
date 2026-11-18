"""/api/v1/pocket: a signed-in member reads the captures and conversations their Pockets kept. Storage: pocket_store.py."""

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

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


# ── conversations from the other surfaces ────────────────────────────────────
# A chat can start on the web (or the TUI, the desktop) and be carried on by a Pocket, and the reverse. The
# member has one open conversation; a Pocket's next turn lands in it. The Pocket itself writes through beam.py.

class TurnIn(BaseModel):
    role: str = Field(pattern="^(user|assistant)$")
    text: str = Field(min_length=1, max_length=8000)


class TurnsIn(BaseModel):
    turns: list[TurnIn] = Field(min_length=1, max_length=20)
    surface: str = "web"


def _surface(value: str) -> str:
    if value not in store.SURFACES or value == "pocket":   # 'pocket' turns come only from a Pocket over the beam
        raise HTTPException(422, "surface must be one of: web, tui, desktop")
    return value


@router.post("/conversations", status_code=201)
async def start_conversation(surface: str = "web", user: dict = Depends(_member)):
    """Open a new conversation (any open one ends). Returns its id; chat into it with /api/v1/chat conversation_id."""
    return {"id": store.new_conversation(user["sub"], surface, _surface(surface))}


@router.post("/conversations/{cid}/turns")
async def add_turns(cid: str, body: TurnsIn, user: dict = Depends(_member)):
    if not store.add_turns(user["sub"], cid, _surface(body.surface), [(t.role, t.text) for t in body.turns]):
        raise HTTPException(404, "No such conversation")
    return {"ok": True}


@router.post("/conversations/{cid}/resume")
async def resume_conversation(cid: str, user: dict = Depends(_member)):
    """Continue this conversation: it becomes the open one, so the next Pocket turn adds to it."""
    if not store.resume(user["sub"], cid):
        raise HTTPException(404, "No such conversation")
    return {"ok": True, "id": cid}
