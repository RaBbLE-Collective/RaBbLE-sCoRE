"""
The node link, cloud side (protocol rabble-node/0, spec: RaBbLE-Grimoire/
RaBbLE-sCoRE/RaBbLE-sCoRE-Local-Pocket-Beam.md sections 6 and 7).

A laptop "node" (server/node_client.py) dials OUT to /api/v1/node and holds
the WebSocket open. When a Pocket beam turn arrives and its owner has a linked,
healthy node, beam.py puts a `node` entry in front of BEAM_LLM_CHAIN; llm.py
calls stream_turn() here, which sends the text turn down the link and waits
for the reply. Any error or timeout raises, and llm.py falls through to the
normal chain for that turn: the Pocket just gets a reply, as always.

Messages (JSON text frames), cloud <-> node:
  node -> cloud  node.hello {node_id, caps, models, v}    first frame
  cloud -> node  node.ready {owner}
  cloud -> node  turn {id, device_id, messages, system, model, deadline_s}
  node -> cloud  turn.reply {id, text, ms} | turn.error {id, code, msg}
  cloud -> node  turn.cancel {id}   cloud gave up; the node kills the run and answers
                                    turn.error {id, code: "cancelled"} (the ack)
  both           keepalive
Text in, text out: nothing else from cloud is ever acted on by the node.

Auth: "Authorization: Bearer <node secret>". P1 accepts one shared secret and
files the node under the "pocket" owner (the identity beam's device token gets).
Per-owner node secrets are P2: add them in owner_for_secret().

Env (cloud):
  SCORE_NODE_SECRET    the node secret, at least 32 chars; unset or shorter = /api/v1/node refuses everyone
  BEAM_NODE_DEADLINE_S seconds to wait for the node before falling back (default 25)

A node stays busy until its turn is answered, acked after a cancel (10 s, then the
link is dropped), or the link drops.

Health: one turn at a time per node. After 2 consecutive errors/timeouts the
node is skipped until its next node.hello or 5 minutes, whichever is first.
"""

import asyncio
import hmac
import json
import logging
import os
import time
import uuid

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

logger = logging.getLogger("rabble.node")
router = APIRouter()

PROTO = "rabble-node/0"
MAX_FAILS = 2
UNHEALTHY_S = 300
HELLO_TIMEOUT_S = 10
CANCEL_ACK_S = 10                     # how long a cancelled turn may hold the node before the link is dropped
MIN_SECRET = 32
MAX_TEXT = 20000                      # chars per message or reply; it is a voice chat


def deadline_s() -> float:
    try:
        return float(os.getenv("BEAM_NODE_DEADLINE_S", "25"))
    except ValueError:
        return 25.0


def owner_for_secret(token: str) -> str | None:
    """The owner (`sub`) a node secret belongs to, or None. P2 adds per-owner secrets here."""
    secret = os.getenv("SCORE_NODE_SECRET", "").strip()
    if secret and len(secret) < MIN_SECRET:
        logger.error("SCORE_NODE_SECRET is shorter than %d chars: node link disabled", MIN_SECRET)
        return None
    if token and secret and hmac.compare_digest(token.encode(), secret.encode()):
        return "pocket"
    return None


class NodeError(RuntimeError):
    pass


class NodeLink:
    def __init__(self, ws: WebSocket, owner: str, node_id: str, models: list):
        self.ws = ws
        self.owner = owner
        self.node_id = node_id
        self.models = models
        self.send_lock = asyncio.Lock()
        self.pending: dict[str, asyncio.Future] = {}
        self.inflight: set[str] = set()                 # turn ids still holding the node
        self.cancelling: dict[str, asyncio.Event] = {}  # cancelled turns waiting for the node's ack
        self.fails = 0
        self.unhealthy_until = 0.0

    @property
    def busy(self) -> bool:
        return bool(self.inflight)

    def reset(self):
        self.fails = 0
        self.unhealthy_until = 0.0

    def healthy(self) -> bool:
        if self.fails >= MAX_FAILS:
            if time.monotonic() < self.unhealthy_until:
                return False
            self.reset()
        return True

    def failed(self):
        self.fails += 1
        if self.fails >= MAX_FAILS:
            self.unhealthy_until = time.monotonic() + UNHEALTHY_S
            logger.warning("node %s unhealthy after %d failures", self.node_id, self.fails)

    async def send(self, msg: dict):
        async with self.send_lock:
            await self.ws.send_text(json.dumps(msg))


# One link per owner (a newer connection replaces an older one).
_links: dict[str, NodeLink] = {}


def link_for(owner: str) -> NodeLink | None:
    link = _links.get(owner)
    return link if link and link.healthy() else None


def node_route(owner: str) -> list[dict]:
    """Chain entries to put ahead of BEAM_LLM_CHAIN: [] unless the owner has a free, healthy node."""
    link = link_for(owner)
    if not link or link.busy:
        return []
    model = (link.models[0] if link.models else "") or "sonnet"
    return [{"provider": "node", "model": str(model), "owner": owner}]


def _text(value, limit: int = MAX_TEXT) -> str:
    return value[:limit] if isinstance(value, str) else ""


_tasks: set[asyncio.Task] = set()


async def _cancel_turn(link: NodeLink, turn_id: str, ack: asyncio.Event):
    """Tell the node to stop a turn the cloud gave up on, and hold it busy until it acks.
    No ack in CANCEL_ACK_S: drop the link (the node reconnects clean)."""
    try:
        try:
            await link.send({"type": "turn.cancel", "id": turn_id})
            await asyncio.wait_for(ack.wait(), timeout=CANCEL_ACK_S)
        except asyncio.TimeoutError:
            logger.warning("node %s did not ack cancel of %s: dropping the link", link.node_id, turn_id)
            await link.ws.close(code=4408, reason="cancel not acked")
        except Exception:
            pass                     # the socket is already gone; the link drop clears busy
    finally:
        link.cancelling.pop(turn_id, None)
        link.inflight.discard(turn_id)


async def stream_turn(candidate: dict, messages: list[dict], system_prompt: str):
    """llm.py's `node` provider: one turn through the owner's node, yielding the reply text once."""
    link = link_for(candidate.get("owner", ""))
    if not link:
        raise NodeError("no healthy node linked")
    if link.busy:
        raise NodeError("node busy")
    turn_id = uuid.uuid4().hex[:12]
    link.inflight.add(turn_id)
    fut = asyncio.get_running_loop().create_future()
    link.pending[turn_id] = fut
    wait = deadline_s()
    try:
        await link.send({
            "type": "turn", "id": turn_id, "device_id": _text(candidate.get("device_id"), 80),
            "messages": [{"role": "assistant" if m.get("role") == "assistant" else "user",
                          "content": _text(m.get("content"))} for m in messages],
            "system": _text(system_prompt), "model": candidate.get("model", ""),
            "deadline_s": wait,
        })
        reply = await asyncio.wait_for(fut, timeout=wait)
    except asyncio.TimeoutError:
        link.failed()
        raise NodeError(f"node timed out after {wait:g}s")
    except NodeError as exc:
        if exc.args and exc.args[0] != "busy":
            link.failed()
        raise
    except Exception as exc:        # the socket died mid-send
        link.failed()
        raise NodeError(f"node link error: {exc}")
    finally:
        link.pending.pop(turn_id, None)
        if fut.done() and not fut.cancelled():
            link.inflight.discard(turn_id)             # the node answered: it is free
        else:
            # timed out, or the caller went away: the run may still be going on the node
            ack = link.cancelling[turn_id] = asyncio.Event()
            task = asyncio.get_running_loop().create_task(_cancel_turn(link, turn_id, ack))
            _tasks.add(task)
            task.add_done_callback(_tasks.discard)
    text = reply.strip()
    if not text:
        link.failed()
        raise NodeError("node returned an empty reply")
    link.reset()
    yield text


def _handle(link: NodeLink, msg: dict):
    """One frame from the node. Only the documented types are acted on."""
    kind = msg.get("type")
    if kind in ("turn.reply", "turn.error"):
        ack = link.cancelling.get(msg.get("id"))
        if ack:
            ack.set()                # the node has stopped (or finished) a turn we cancelled
            return
        fut = link.pending.get(msg.get("id"))
        if not fut or fut.done():
            return                   # late answer to a turn already given up on
        if kind == "turn.reply":
            fut.set_result(_text(msg.get("text")))
        else:
            code = msg.get("code")
            fut.set_exception(NodeError("busy" if code == "busy" else f"node {code}: {_text(msg.get('msg'), 200)}"))
    elif kind == "node.hello":
        link.reset()                 # a re-hello clears the unhealthy mark
    # keepalive and anything else: ignored


def _release(link: NodeLink, why: str):
    """The link is gone: fail its waiting turns (they fall back) and free it."""
    for fut in link.pending.values():
        if not fut.done():
            fut.set_exception(NodeError(why))
    link.inflight.clear()
    for ack in link.cancelling.values():
        ack.set()


async def _recv_text(ws: WebSocket) -> str | None:
    """The next text frame; None for a binary or otherwise unusable one; raises on disconnect."""
    message = await ws.receive()
    if message.get("type") == "websocket.disconnect":
        raise WebSocketDisconnect(message.get("code", 1000))
    text = message.get("text")
    return text if isinstance(text, str) else None


@router.websocket("/api/v1/node")
async def node_link(ws: WebSocket):
    header = ws.headers.get("authorization", "")
    token = header[7:].strip() if header.lower().startswith("bearer ") else ""
    owner = owner_for_secret(token)
    if not owner:
        logger.warning("node refused from %s", ws.client.host if ws.client else "?")
        await ws.close(code=4401, reason="node secret required")
        return
    await ws.accept()
    link = None
    try:
        try:
            hello = json.loads(await asyncio.wait_for(_recv_text(ws), timeout=HELLO_TIMEOUT_S) or "")
        except (TypeError, ValueError):
            hello = None
        if not isinstance(hello, dict) or hello.get("type") != "node.hello":
            await ws.close(code=4400, reason="node.hello expected")
            return
        models = [m for m in hello.get("models", []) if isinstance(m, str)] if isinstance(hello.get("models"), list) else []
        link = NodeLink(ws, owner, _text(hello.get("node_id"), 64) or "node", models)
        old = _links.get(owner)
        _links[owner] = link
        if old:
            _release(old, "node replaced by a newer link")   # its in-flight turn falls back now
            try:
                await old.ws.close(code=4409, reason="replaced by a newer node")
            except Exception:
                pass
        await link.send({"type": "node.ready", "owner": owner})
        logger.info("node linked id=%s owner=%s", link.node_id, owner)
        while True:
            raw = await _recv_text(ws)          # a binary frame comes back as None
            try:
                msg = json.loads(raw) if raw is not None else None
                if isinstance(msg, dict):
                    if msg.get("type") == "keepalive":
                        await link.send({"type": "keepalive"})
                    _handle(link, msg)
            except WebSocketDisconnect:
                raise
            except Exception as exc:            # a bad frame never takes the link down
                logger.warning("node %s bad frame ignored: %r", link.node_id, exc)
    except (WebSocketDisconnect, asyncio.TimeoutError, RuntimeError):
        pass
    finally:
        if link:
            if _links.get(owner) is link:
                del _links[owner]
            _release(link, "node disconnected")
            logger.info("node closed id=%s", link.node_id)
