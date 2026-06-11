import os, json
from dotenv import load_dotenv
load_dotenv()

import logging
from fastapi import FastAPI, Depends, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, JSONResponse
from pydantic import BaseModel, field_validator

import auth
import llm
import agents
import audit
import rate_limit
import workflows as wf_store
import users as user_store
import sessions as session_store
from auth_routes import router as auth_router, member_router

logger = logging.getLogger("rabble")
logging.basicConfig(level=logging.INFO)

app = FastAPI(
    title="RaBbLE",
    description="Benevolent Behavioral Learning Engine — Agentic Intelligence API",
    version="v0.0.0.0",
)


def _cors_origins() -> list[str]:
    raw = os.getenv("FRONTEND_URL", "*").strip()
    if not raw or raw == "*":
        return ["*"]
    return [o.strip() for o in raw.split(",") if o.strip()]


app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins(),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth_router)
app.include_router(member_router)


# ── Request models ─────────────────────────────────────────────────────────────

class ChatRequest(BaseModel):
    messages:      list[dict]
    model_tier:    str = "auto"
    workflow_type: str = "default"

    @field_validator("model_tier")
    @classmethod
    def _validate_tier(cls, v: str) -> str:
        if v not in {"auto", "fast", "medium", "strong"}:
            raise ValueError("model_tier must be one of: auto, fast, medium, strong")
        return v

    @field_validator("workflow_type")
    @classmethod
    def _validate_workflow_type(cls, v: str) -> str:
        valid = set(agents.WORKFLOW_TYPES) | {"default"}
        if v not in valid:
            raise ValueError(f"workflow_type must be one of: {', '.join(sorted(valid))}")
        return v


class WorkflowCreateRequest(BaseModel):
    goal:          str
    workflow_type: str = "default"
    model_tier:    str = "medium"

    @field_validator("model_tier")
    @classmethod
    def _validate_tier(cls, v: str) -> str:
        if v not in {"fast", "medium", "strong"}:
            raise ValueError("model_tier must be fast, medium, or strong")
        return v

    @field_validator("workflow_type")
    @classmethod
    def _validate_workflow_type(cls, v: str) -> str:
        valid = set(agents.WORKFLOW_TYPES) | {"default"}
        if v not in valid:
            raise ValueError(f"workflow_type must be one of: {', '.join(sorted(valid))}")
        return v


class WorkflowStepRequest(BaseModel):
    message: str


class SessionCreateRequest(BaseModel):
    resume: bool = True


class SessionMessageRequest(BaseModel):
    content: str
    model_tier: str = "auto"


def build_session_prompt(user: dict, workflow_type: str = "default") -> str:
    base = agents.rabble_system_prompt(workflow_type)
    intention = user.get("intention", "")
    handle = user.get("handle", "")
    if intention:
        base += f"\n\n---\nYou are speaking with {handle}. Their intention: {intention}"
    return base


def _load_user_profile(user_token: dict) -> dict:
    """
    Merge file-backed profile data into the token claims dict.
    Decrypts BYO key if present so resolve_user_chain can use it.
    Falls back to token-only data if profile not found.
    """
    profile = user_store.get_user(user_token["sub"])
    if not profile:
        return user_token
    merged = {**user_token, **{
        "handle":      profile.handle,
        "intention":   profile.intention,
        "llm_backend": profile.llm_backend,
        "tier":        profile.tier,
    }}
    if profile.byo_key_enc:
        try:
            merged["byo_key_plain"] = user_store.decrypt_key(profile.byo_key_enc)
        except Exception:
            pass
    return merged


# ── Health ─────────────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {"status": "ok", "entity": "RaBbLE", "version": "v0.0.0.0"}


# ── Status ─────────────────────────────────────────────────────────────────────

@app.get("/api/v1/status")
async def status(request: Request, user: dict = Depends(auth.require_user)):
    rate_limit.apply_ip_limit(request.client.host)
    return {
        "user": {
            "id":   user["sub"],
            "name": user["name"],
            "tier": user.get("tier", "free"),
        },
        "providers":      ["groq", "openrouter"],
        "workflow_types": agents.WORKFLOW_TYPES,
        "model_tiers":    ["fast", "medium", "strong"],
        "demo_mode":      auth.DEMO_MODE,
    }


# ── Chat — streaming ───────────────────────────────────────────────────────────

@app.post("/api/v1/chat")
async def chat(
    req:     ChatRequest,
    request: Request,
    user:    dict = Depends(auth.require_user),
):
    user_id   = user["sub"]
    user_tier = user.get("tier", "free")

    if req.model_tier == "auto":
        routed_tier, detected_wf = agents.classify_request(req.messages)
        wf_type = req.workflow_type if req.workflow_type != "default" else detected_wf
    else:
        routed_tier = req.model_tier
        wf_type     = req.workflow_type

    rate_limit.apply_user_limit(user_id, routed_tier, user_tier)

    user_profile = _load_user_profile(user)
    system_prompt = agents.rabble_system_prompt(wf_type)
    logger.info("chat user=%s tier=%s wf=%s", user_id, routed_tier, wf_type)
    audit.log(user_id=user_id, action="chat", tier=routed_tier, workflow_type=wf_type, status=0)

    async def _stream():
        try:
            async for chunk in llm.stream_chat(req.messages, system_prompt, model_tier=routed_tier, user=user_profile):
                yield f"data: {json.dumps(chunk)}\n\n"
            yield "data: [DONE]\n\n"
            audit.log(user_id=user_id, action="chat", tier=routed_tier, workflow_type=wf_type, status=200)
        except Exception as exc:
            logger.error("chat stream error user=%s: %s", user_id, exc)
            audit.log(user_id=user_id, action="chat", tier=routed_tier, workflow_type=wf_type, status=500, error=str(exc))
            yield f"data: {json.dumps('[RaBbLE: all providers unreachable — ' + str(exc) + ']')}\n\n"
            yield "data: [DONE]\n\n"

    return StreamingResponse(_stream(), media_type="text/event-stream")


# ── Chat — non-streaming ───────────────────────────────────────────────────────

@app.post("/api/v1/chat/complete")
async def chat_complete(
    req:     ChatRequest,
    request: Request,
    user:    dict = Depends(auth.require_user),
):
    user_id   = user["sub"]
    user_tier = user.get("tier", "free")

    if req.model_tier == "auto":
        routed_tier, detected_wf = agents.classify_request(req.messages)
        wf_type = req.workflow_type if req.workflow_type != "default" else detected_wf
    else:
        routed_tier = req.model_tier
        wf_type     = req.workflow_type

    rate_limit.apply_user_limit(user_id, routed_tier, user_tier)

    user_profile = _load_user_profile(user)
    system_prompt = agents.rabble_system_prompt(wf_type)
    try:
        content = await llm.complete_chat(req.messages, system_prompt, model_tier=routed_tier, user=user_profile)
        audit.log(user_id=user_id, action="chat_complete", tier=routed_tier, workflow_type=wf_type, status=200)
        return {"content": content, "workflow_type": wf_type, "tier": routed_tier}
    except Exception as exc:
        audit.log(user_id=user_id, action="chat_complete", tier=routed_tier, workflow_type=wf_type, status=500, error=str(exc))
        raise HTTPException(502, f"LLM error: {exc}")


# ── Workflows ──────────────────────────────────────────────────────────────────

@app.post("/api/v1/workflows", status_code=201)
async def create_workflow(
    req:     WorkflowCreateRequest,
    request: Request,
    user:    dict = Depends(auth.require_user),
):
    user_id   = user["sub"]
    user_tier = user.get("tier", "free")

    rate_limit.apply_user_limit(user_id, req.model_tier, user_tier)

    record = wf_store.create(user_id, req.workflow_type, req.goal, req.model_tier)
    wf_id  = record["id"]

    init_content = f"Begin a {req.workflow_type} workflow. The goal is: {req.goal}"
    wf_store.add_message(wf_id, "user", init_content)
    wf_store.set_state(wf_id, "running")

    system_prompt = agents.rabble_system_prompt(req.workflow_type)
    try:
        response = await llm.complete_chat(
            [{"role": "user", "content": init_content}],
            system_prompt,
            model_tier=req.model_tier,
        )
        wf_store.add_message(wf_id, "assistant", response)
        wf_store.set_state(
            wf_id, "awaiting_input",
            step={"step": 0, "type": "init", "response": response},
            result=response,
        )
        audit.log(user_id=user_id, action="workflow_create", workflow_type=req.workflow_type, tier=req.model_tier, status=201)
    except Exception as exc:
        wf_store.set_state(wf_id, "failed", result=str(exc))
        audit.log(user_id=user_id, action="workflow_create", workflow_type=req.workflow_type, tier=req.model_tier, status=502, error=str(exc))
        raise HTTPException(502, f"Workflow init failed: {exc}")

    record = wf_store.get(wf_id, user_id)
    return {
        "id":         wf_id,
        "type":       record["type"],
        "state":      record["state"],
        "goal":       record["goal"],
        "response":   record["result"],
        "step_count": len(record["steps"]),
    }


@app.get("/api/v1/workflows/{wf_id}")
async def get_workflow(wf_id: str, user: dict = Depends(auth.require_user)):
    record = wf_store.get(wf_id, user["sub"])
    return {
        "id":         record["id"],
        "type":       record["type"],
        "state":      record["state"],
        "goal":       record["goal"],
        "messages":   record["messages"],
        "steps":      record["steps"],
        "result":     record["result"],
        "step_count": len(record["steps"]),
        "created_at": record["created_at"],
        "updated_at": record["updated_at"],
    }


@app.post("/api/v1/workflows/{wf_id}/step")
async def workflow_step(
    wf_id:   str,
    req:     WorkflowStepRequest,
    request: Request,
    user:    dict = Depends(auth.require_user),
):
    user_id   = user["sub"]
    user_tier = user.get("tier", "free")

    record = wf_store.get(wf_id, user_id)
    if record["state"] not in ("awaiting_input", "pending"):
        raise HTTPException(400, f"Workflow is '{record['state']}' — cannot accept input now")

    rate_limit.apply_user_limit(user_id, record["model_tier"], user_tier)

    wf_store.add_message(wf_id, "user", req.message)
    wf_store.set_state(wf_id, "running")

    system_prompt = agents.rabble_system_prompt(record["type"])
    messages = wf_store.get(wf_id, user_id)["messages"]

    try:
        response = await llm.complete_chat(messages, system_prompt, model_tier=record["model_tier"])
        step_num = len(record["steps"])
        wf_store.add_message(wf_id, "assistant", response)
        wf_store.set_state(
            wf_id, "awaiting_input",
            step={"step": step_num, "type": "exchange", "response": response},
            result=response,
        )
        audit.log(user_id=user_id, action="workflow_step", workflow_type=record["type"], tier=record["model_tier"], status=200)
        return {
            "id":       wf_id,
            "state":    "awaiting_input",
            "response": response,
            "step":     step_num,
        }
    except Exception as exc:
        wf_store.set_state(wf_id, "failed", result=str(exc))
        audit.log(user_id=user_id, action="workflow_step", workflow_type=record["type"], tier=record["model_tier"], status=502, error=str(exc))
        raise HTTPException(502, f"Workflow step failed: {exc}")


@app.delete("/api/v1/workflows/{wf_id}")
async def cancel_workflow(wf_id: str, user: dict = Depends(auth.require_user)):
    wf_store.cancel(wf_id, user["sub"])
    return {"status": "cancelled", "id": wf_id}


# ── Sessions ───────────────────────────────────────────────────────────────────

def _session_summary(rec: "session_store.SessionRecord") -> dict:
    return {
        "session_id":    rec.session_id,
        "title":         rec.title,
        "created_at":    rec.created_at,
        "last_active":   rec.last_active,
        "message_count": len(rec.messages),
    }


@app.post("/api/v1/sessions")
async def create_or_resume_session(
    req:  SessionCreateRequest,
    user: dict = Depends(auth.require_user),
):
    user_id = user["sub"]
    if req.resume:
        rec = session_store.get_or_create_default(user_id)
        status_code = 200 if rec.messages else 201
    else:
        rec = session_store.create_session(user_id)
        status_code = 201

    from fastapi.responses import JSONResponse
    return JSONResponse(content=_session_summary(rec), status_code=status_code)


@app.get("/api/v1/sessions")
async def list_sessions(user: dict = Depends(auth.require_user)):
    records = session_store.list_user_sessions(user["sub"])
    return [_session_summary(r) for r in records]


@app.get("/api/v1/sessions/{session_id}")
async def get_session(session_id: str, user: dict = Depends(auth.require_user)):
    rec = session_store.get_session(session_id, user["sub"])
    if not rec:
        raise HTTPException(404, "Session not found")
    return rec.model_dump()


@app.post("/api/v1/sessions/{session_id}/message")
async def session_message(
    session_id: str,
    req:        SessionMessageRequest,
    request:    Request,
    user:       dict = Depends(auth.require_user),
):
    user_id = user["sub"]
    rec = session_store.get_session(session_id, user_id)
    if not rec:
        raise HTTPException(404, "Session not found")

    if req.model_tier not in {"auto", "fast", "medium", "strong"}:
        raise HTTPException(422, "model_tier must be one of: auto, fast, medium, strong")

    user_profile = _load_user_profile(user)

    if req.model_tier == "auto":
        routed_tier, _ = agents.classify_request([{"role": "user", "content": req.content}])
    else:
        routed_tier = req.model_tier

    rate_limit.apply_user_limit(user_id, routed_tier, user.get("tier", "free"))

    session_store.append_message(session_id, user_id, "user", req.content)

    history = [{"role": m.role, "content": m.content} for m in rec.messages]
    history.append({"role": "user", "content": req.content})

    system_prompt = build_session_prompt(user_profile)
    audit.log(user_id=user_id, action="session_message", tier=routed_tier, status=0)

    async def _stream():
        assistant_chunks = []
        try:
            async for chunk in llm.stream_chat(history, system_prompt, model_tier=routed_tier, user=user_profile):
                assistant_chunks.append(chunk)
                yield f"data: {json.dumps(chunk)}\n\n"
            full_response = "".join(assistant_chunks)
            session_store.append_message(session_id, user_id, "assistant", full_response)
            yield "data: [DONE]\n\n"
            audit.log(user_id=user_id, action="session_message", tier=routed_tier, status=200)
        except Exception as exc:
            logger.error("session stream error user=%s session=%s: %s", user_id, session_id, exc)
            audit.log(user_id=user_id, action="session_message", tier=routed_tier, status=500, error=str(exc))
            yield f"data: {json.dumps('[RaBbLE: all providers unreachable — ' + str(exc) + ']')}\n\n"
            yield "data: [DONE]\n\n"

    return StreamingResponse(_stream(), media_type="text/event-stream")
