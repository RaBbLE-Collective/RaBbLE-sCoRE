import os
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
from auth_routes import router as auth_router

logger = logging.getLogger("rabble")
logging.basicConfig(level=logging.INFO)

app = FastAPI(
    title="RaBbLE",
    description="Benevolent Behavioral Learning Engine — Agentic Intelligence API",
    version="1.0.0",
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


# ── Health ─────────────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {"status": "ok", "entity": "RaBbLE", "version": "1.0.0"}


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

    system_prompt = agents.rabble_system_prompt(wf_type)
    logger.info("chat user=%s tier=%s wf=%s", user_id, routed_tier, wf_type)
    audit.log(user_id=user_id, action="chat", tier=routed_tier, workflow_type=wf_type, status=0)

    async def _stream():
        try:
            async for chunk in llm.stream_chat(req.messages, system_prompt, model_tier=routed_tier):
                yield chunk
            audit.log(user_id=user_id, action="chat", tier=routed_tier, workflow_type=wf_type, status=200)
        except Exception as exc:
            logger.error("chat stream error user=%s: %s", user_id, exc)
            audit.log(user_id=user_id, action="chat", tier=routed_tier, workflow_type=wf_type, status=500, error=str(exc))
            yield f"\n[RaBbLE encountered turbulence: {exc}]"

    return StreamingResponse(_stream(), media_type="text/plain")


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

    system_prompt = agents.rabble_system_prompt(wf_type)
    try:
        content = await llm.complete_chat(req.messages, system_prompt, model_tier=routed_tier)
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
