"""
The node link, laptop side (protocol rabble-node/0; cloud side is node.py).

Dials OUT to cloud sCoRE, holds the WebSocket open, and runs one sandboxed
Claude Code turn at a time for the Pocket. Listens on nothing. Text in, text
out: only `turn` frames are acted on, and only their `messages` / `system`
text (and a model name, if CC_MODEL is unset) are used. The tool list, cwd and
flags always come from this machine's env, never from cloud.

Run (from RaBbLE-sCoRE/server):
  SCORE_NODE_URL=wss://score.joinrabble.world/api/v1/node \\
  SCORE_NODE_SECRET=... CC_CONTEXT_DIR=~/.local/share/rabble/beam-context \\
  .venv/bin/python -m node_client

Env:
  SCORE_NODE_URL       the cloud endpoint (default ws://127.0.0.1:8083/api/v1/node, local dev);
                       plain ws:// is refused for any host but loopback
  SCORE_NODE_ID        this node's name in logs (default: the hostname)
  SCORE_NODE_SECRET    must equal the cloud's SCORE_NODE_SECRET, at least 32 chars (never logged)
  CC_COMMAND           the claude command, may be a wrapper (default claude)
  CC_MODEL             model for every turn (default sonnet; unset = use cloud's request)
  CC_TOOLS             built-in tools Claude may have (default WebSearch,WebFetch)
  CC_CONTEXT_DIR       REQUIRED. cwd for Claude: an existing curated dir with NO secrets, not inside
                       any git work tree. CC_WORKDIR and the process cwd are never used here.
                       Claude runs with a scrubbed environment (llm.scrubbed_env)
  BEAM_NODE_DEADLINE_S upper bound per turn in seconds (default 25; cloud's request is capped to it).
                       Claude gets deadline - 3 s (min 5) so the node answers before the cloud gives up.
A `turn.cancel {id}` frame from cloud kills that turn's Claude process; the node acks with
turn.error {id, code: "cancelled"}.
Reconnects with backoff 2 s -> 60 s. Logs turn ids and timings, never text.
"""

import asyncio
import json
import logging
import os
import re
import socket
import time
from urllib.parse import urlsplit

import websockets

import llm

logger = logging.getLogger("rabble.node_client")

BACKOFF_MIN, BACKOFF_MAX = 2.0, 60.0
KEEPALIVE_S = 30
MAX_REPLY = 4000                      # chars: it is spoken aloud
MODEL_RE = re.compile(r"^[A-Za-z0-9._\-\[\]]{1,64}$")
MIN_SECRET = 32
LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}
CLAUDE_MARGIN_S, CLAUDE_MIN_S = 3.0, 5.0


def context_dir() -> str:
    """The cwd for every Claude turn.  CC_CONTEXT_DIR must be set, exist, and sit outside
    every git work tree; CC_WORKDIR and the process cwd are never a fallback."""
    raw = os.getenv("CC_CONTEXT_DIR", "").strip()
    if not raw:
        raise RuntimeError("CC_CONTEXT_DIR is not set: the node needs a curated, secret-free directory")
    path = os.path.realpath(os.path.expanduser(raw))
    if not os.path.isdir(path):
        raise RuntimeError(f"CC_CONTEXT_DIR {raw} does not exist or is not a directory")
    probe = path
    while True:
        if os.path.exists(os.path.join(probe, ".git")):
            raise RuntimeError(f"CC_CONTEXT_DIR {raw} is inside a git work tree ({probe}): use a directory outside any repo")
        parent = os.path.dirname(probe)
        if parent == probe:
            return path
        probe = parent


def check_url(url: str) -> str:
    parts = urlsplit(url)
    if parts.scheme == "wss" and parts.hostname:
        return url
    if parts.scheme == "ws" and parts.hostname in LOOPBACK_HOSTS:
        return url
    raise RuntimeError("SCORE_NODE_URL must be wss://, or ws:// to a loopback host only")


def check_secret(secret: str) -> str:
    if len(secret) < MIN_SECRET:
        raise RuntimeError(f"SCORE_NODE_SECRET must be at least {MIN_SECRET} chars")
    return secret


def _model(requested) -> str:
    own = os.getenv("CC_MODEL", "").strip()
    if own:
        return own
    return requested if isinstance(requested, str) and MODEL_RE.match(requested) else "sonnet"


def _deadline(requested) -> float:
    cap = float(os.getenv("BEAM_NODE_DEADLINE_S", "25") or 25)
    return min(requested, cap) if isinstance(requested, (int, float)) and requested > 0 else cap


def _messages(raw) -> list[dict]:
    out = []
    for m in raw if isinstance(raw, list) else []:
        if isinstance(m, dict) and isinstance(m.get("content"), str):
            out.append({"role": "assistant" if m.get("role") == "assistant" else "user",
                        "content": m["content"]})
    return out


async def run_claude(messages: list[dict], system: str, model: str, timeout: float) -> str:
    """One sandboxed Claude Code turn: llm.py's argv builder, so the same flags as everywhere,
    in the curated context dir and a scrubbed environment.  The process dies with the turn
    (timeout or cancel)."""
    provider = llm.BUILTIN_PROVIDERS["claude_code"]
    provider = {**provider, "command": os.getenv("CC_COMMAND", "claude")}
    argv = llm._subprocess_argv(provider, model, system, llm._format_messages_for_cli(messages))
    proc = await asyncio.create_subprocess_exec(
        *argv, stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        cwd=context_dir(), env=llm.scrubbed_env())
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except BaseException:                  # timeout, cancel, anything: never leave claude running
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        await asyncio.shield(proc.wait())
        raise
    if proc.returncode != 0:
        raise RuntimeError(f"claude exited {proc.returncode}: {stderr.decode()[:200]}")
    return stdout.decode().strip()[:MAX_REPLY]


async def handle_turn(ws, msg: dict, busy: asyncio.Lock):
    turn_id = msg.get("id")
    if not isinstance(turn_id, str):
        return
    if busy.locked():
        await ws.send(json.dumps({"type": "turn.error", "id": turn_id, "code": "busy", "msg": "one turn at a time"}))
        return
    async with busy:
        t0 = time.monotonic()
        try:
            messages = _messages(msg.get("messages"))
            if not messages:
                raise RuntimeError("no messages")
            system = msg.get("system") if isinstance(msg.get("system"), str) else ""
            claude_s = max(_deadline(msg.get("deadline_s")) - CLAUDE_MARGIN_S, CLAUDE_MIN_S)
            text = await run_claude(messages, system, _model(msg.get("model")), claude_s)
            if not text:
                raise RuntimeError("empty reply")
            out = {"type": "turn.reply", "id": turn_id, "text": text, "ms": int((time.monotonic() - t0) * 1000)}
        except asyncio.TimeoutError:
            out = {"type": "turn.error", "id": turn_id, "code": "timeout", "msg": "claude timed out"}
        except asyncio.CancelledError:
            # turn.cancel (or the link dropping): claude is already dead; ack so the cloud frees us
            logger.info("turn %s cancelled %dms", turn_id, int((time.monotonic() - t0) * 1000))
            try:
                await ws.send(json.dumps({"type": "turn.error", "id": turn_id, "code": "cancelled", "msg": "cancelled"}))
            except Exception:
                pass
            raise
        except Exception as exc:
            out = {"type": "turn.error", "id": turn_id, "code": "cli", "msg": str(exc)[:200]}
        logger.info("turn %s %s %dms", turn_id, out["type"], int((time.monotonic() - t0) * 1000))
        await ws.send(json.dumps(out))


async def session(url: str, secret: str, node_id: str):
    """One connection. Returns when it drops; raises if it could not connect."""
    async with websockets.connect(url, additional_headers={"Authorization": f"Bearer {secret}"},
                                  ping_interval=None) as ws:
        await ws.send(json.dumps({"type": "node.hello", "node_id": node_id, "caps": ["claude_code"],
                                  "models": [os.getenv("CC_MODEL", "").strip() or "sonnet"], "v": 0}))
        ready = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
        if ready.get("type") != "node.ready":
            raise RuntimeError("cloud did not send node.ready")
        logger.info("linked as %s", node_id)

        async def keepalive():
            while True:
                await asyncio.sleep(KEEPALIVE_S)
                await ws.send(json.dumps({"type": "keepalive"}))

        busy = asyncio.Lock()
        tasks: set[asyncio.Task] = set()
        turns: dict[str, asyncio.Task] = {}      # turn id -> its task, for turn.cancel
        pinger = asyncio.create_task(keepalive())
        try:
            async for raw in ws:
                try:
                    msg = json.loads(raw)       # a binary frame is bytes: json.loads decodes or raises
                except (ValueError, TypeError):
                    continue
                if not isinstance(msg, dict):
                    continue
                if msg.get("type") == "turn" and isinstance(msg.get("id"), str):
                    tid = msg["id"]
                    t = asyncio.create_task(handle_turn(ws, msg, busy))
                    tasks.add(t)
                    if not busy.locked():       # a refused (busy) turn must not shadow the running one
                        turns[tid] = t
                    t.add_done_callback(tasks.discard)
                    t.add_done_callback(lambda _t, tid=tid: turns.pop(tid, None) if turns.get(tid) is _t else None)
                elif msg.get("type") == "turn.cancel":
                    t = turns.get(msg.get("id")) if isinstance(msg.get("id"), str) else None
                    if t:
                        t.cancel()
                # everything else (keepalive, unknown types) is ignored
        finally:
            pinger.cancel()
            for t in tasks:
                t.cancel()


async def main():
    secret = os.getenv("SCORE_NODE_SECRET", "").strip()
    if not secret:
        raise SystemExit("SCORE_NODE_SECRET is not set")
    try:
        check_secret(secret)
        check_url(os.getenv("SCORE_NODE_URL", "ws://127.0.0.1:8083/api/v1/node"))
        context_dir()
    except RuntimeError as exc:
        raise SystemExit(str(exc))
    url = os.getenv("SCORE_NODE_URL", "ws://127.0.0.1:8083/api/v1/node")
    node_id = os.getenv("SCORE_NODE_ID", "").strip() or socket.gethostname()
    delay = BACKOFF_MIN
    while True:
        started = time.monotonic()
        try:
            await session(url, secret, node_id)
            logger.warning("link closed")
        except (OSError, websockets.WebSocketException, RuntimeError, asyncio.TimeoutError) as exc:
            logger.warning("link down: %s", exc)
        held = time.monotonic() - started > BACKOFF_MAX
        await asyncio.sleep(BACKOFF_MIN if held else delay)
        # a link that held for a while starts the backoff over
        delay = BACKOFF_MIN if held else min(delay * 2, BACKOFF_MAX)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    asyncio.run(main())
