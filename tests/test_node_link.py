"""Node link (server/node.py, node_client.py, beam routing). No network: a fake
node talks over the FastAPI TestClient websocket. Run: server/.venv/bin/python -m pytest tests"""

import asyncio
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))
os.environ.setdefault("JWT_SECRET", "test-secret-not-real-0123456789")

import llm  # noqa: E402
import node  # noqa: E402
import node_client  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from starlette.websockets import WebSocketDisconnect  # noqa: E402

SECRET = "node-secret-for-tests-0123456789abcdef"
HELLO = {"type": "node.hello", "node_id": "t", "caps": ["claude_code"], "models": ["sonnet"], "v": 0}


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("SCORE_NODE_SECRET", SECRET)
    node._links.clear()
    yield
    node._links.clear()


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(node.router)
    return TestClient(app)


def connect(client, secret=SECRET):
    headers = {"Authorization": f"Bearer {secret}"} if secret is not None else {}
    return client.websocket_connect("/api/v1/node", headers=headers)


def link_up(ws):
    ws.send_text(json.dumps(HELLO))
    assert ws.receive_json() == {"type": "node.ready", "owner": "pocket"}


def test_refused_without_or_wrong_secret(client):
    for bad in (None, "wrong", ""):
        with pytest.raises(WebSocketDisconnect):
            with connect(client, bad) as ws:
                ws.receive_text()


def test_refused_when_secret_unset(client, monkeypatch):
    monkeypatch.delenv("SCORE_NODE_SECRET")
    with pytest.raises(WebSocketDisconnect):
        with connect(client, "") as ws:
            ws.receive_text()
    with pytest.raises(WebSocketDisconnect):
        with connect(client, SECRET) as ws:
            ws.receive_text()


OWNER = {"sub": "pocket"}
NODE_CHAIN = [{"provider": "node", "model": "sonnet", "owner": "pocket", "device_id": "dev1"}]
FALLBACK = [{"provider": "claude_code", "model": "m"}]   # stands in for BEAM_LLM_CHAIN


async def collect(chain, text="hi"):
    out = []
    async for c in llm.stream_chat([{"role": "user", "content": text}], "sys", model_tier="fast", chain=chain, user=OWNER):
        out.append(c)
    return "".join(out)


@pytest.fixture
def app_client(monkeypatch):
    """TestClient whose portal loop also runs node.py, so a test can drive a turn
    through /run while a fake node answers on the websocket from the test thread."""
    app = FastAPI()
    app.include_router(node.router)

    @app.get("/run")
    async def run(text: str = "hi"):
        try:
            return {"text": await collect(NODE_CHAIN + FALLBACK, text)}
        except Exception as exc:
            return {"error": str(exc)}

    async def fallback(candidate, messages, system):
        yield "from the chain"
    monkeypatch.setattr(llm, "_stream_subprocess", fallback)
    with TestClient(app) as c:
        yield c


def start_run(client, text="hi"):
    import concurrent.futures
    pool = concurrent.futures.ThreadPoolExecutor(1)
    return pool.submit(lambda: client.get("/run", params={"text": text}).json())


def test_turn_round_trips(app_client):
    with connect(app_client) as ws:
        link_up(ws)
        assert node.node_route("pocket")[0]["provider"] == "node"
        fut = start_run(app_client)
        turn = ws.receive_json()
        assert turn["type"] == "turn" and turn["device_id"] == "dev1"
        assert turn["messages"] == [{"role": "user", "content": "hi"}]
        assert turn["system"] == "sys" and turn["deadline_s"] == 25.0
        ws.send_json({"type": "turn.reply", "id": turn["id"], "text": " hello there ", "ms": 5})
        assert fut.result(5) == {"text": "hello there"}
        assert node.node_route("pocket")          # healthy and idle again


def test_timeout_falls_back_to_chain(app_client, monkeypatch):
    monkeypatch.setenv("BEAM_NODE_DEADLINE_S", "0.2")
    with connect(app_client) as ws:
        link_up(ws)
        fut = start_run(app_client)
        ws.receive_json()                          # the node never answers
        res = fut.result(5)
        assert res == {"text": "from the chain"}
        assert node._links["pocket"].fails == 1


def test_node_error_falls_back_and_two_failures_unhealthy(app_client, monkeypatch):
    with connect(app_client) as ws:
        link_up(ws)
        for _ in range(2):
            assert node.node_route("pocket")
            fut = start_run(app_client)
            turn = ws.receive_json()
            ws.send_json({"type": "turn.error", "id": turn["id"], "code": "cli", "msg": "boom"})
            assert fut.result(5) == {"text": "from the chain"}
        assert node.node_route("pocket") == []     # unhealthy: not routed
        ws.send_json(HELLO)                        # a new hello clears it
        for _ in range(50):
            if node.node_route("pocket"):
                break
            import time; time.sleep(0.05)
        assert node.node_route("pocket")


def test_unhealthy_expires_after_five_minutes(app_client, monkeypatch):
    with connect(app_client) as ws:
        link_up(ws)
        lk = node._links["pocket"]
        lk.failed(); lk.failed()
        assert node.node_route("pocket") == []
        lk.unhealthy_until = 0
        assert node.node_route("pocket")


def test_no_node_means_no_route():
    assert node.node_route("pocket") == []


def test_node_ignores_non_turn_and_never_takes_tools(monkeypatch):
    # client side: unknown types and tool fields from cloud are never used
    assert node_client._messages([{"role": "system", "content": "x"}, {"role": "user", "content": 5}]) == [
        {"role": "user", "content": "x"}]
    monkeypatch.setenv("CC_MODEL", "haiku")
    assert node_client._model("opus; rm -rf") == "haiku"
    monkeypatch.delenv("CC_MODEL")
    assert node_client._model("opus; rm -rf") == "sonnet"
    monkeypatch.setenv("BEAM_NODE_DEADLINE_S", "25")
    assert node_client._deadline(999) == 25 and node_client._deadline(5) == 5


def test_client_runs_sandboxed_argv(monkeypatch, tmp_path):
    seen = {}

    class P:
        returncode = 0
        async def communicate(self):
            return b" the reply \n", b""

    async def fake_exec(*argv, **kw):
        seen["argv"], seen["cwd"], seen["env"] = argv, kw.get("cwd"), kw.get("env")
        return P()
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setenv("CC_CONTEXT_DIR", str(tmp_path))
    out = asyncio.run(node_client.run_claude([{"role": "user", "content": "yo"}], "sys", "haiku", 5))
    assert out == "the reply" and seen["cwd"] == os.path.realpath(tmp_path)
    assert "--tools" in seen["argv"] and "--disallowedTools" in seen["argv"]


def test_guest_never_gets_cli_provider(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "x")
    for user in (None, {"sub": "guest", "llm_backend": "hosted_groq"}, {"sub": "guest"}):
        for tier in llm.DEFAULT_MODEL_CHAINS:
            try:
                got = llm._resolve_candidates(tier, None, user)
            except RuntimeError:
                continue                           # nothing left is fine; a CLI is not
            assert got and not any(llm._is_cli(c["provider"]) for c in got), (user, tier)
    # a named guest cannot smuggle a CLI in through an explicit chain either
    with pytest.raises(RuntimeError):
        llm._resolve_candidates("fast", [{"provider": "claude_code", "model": "haiku"}], {"sub": "guest"})
    # an anonymous caller does not get one from an explicit chain any more
    with pytest.raises(RuntimeError):
        llm._resolve_candidates("fast", [{"provider": "claude_code", "model": "haiku"}], None)
    # the owner identity does
    assert llm._resolve_candidates("fast", [{"provider": "claude_code", "model": "haiku"}], {"sub": "pocket"})
    assert llm._without_cli(llm.resolve_user_chain({"sub": "g", "llm_backend": "hosted_groq"}, "fast"))
