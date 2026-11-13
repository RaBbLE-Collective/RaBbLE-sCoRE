"""Security hardening for the P0/P1 beam + node work (llm.py, beam.py, node.py,
node_client.py, auth_routes.py, main.py). No network, no claude call.
Run: server/.venv/bin/python -m pytest tests"""

import asyncio
import json
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))
os.environ.setdefault("JWT_SECRET", "test-secret-not-real-0123456789")

import llm  # noqa: E402
import node  # noqa: E402
import node_client  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

SECRET = "node-secret-for-tests-0123456789abcdef"
HELLO = {"type": "node.hello", "node_id": "t", "caps": ["claude_code"], "models": ["sonnet"], "v": 0}
OWNER = {"sub": "pocket"}
MEMBER = {"sub": "u-member-1", "name": "Member", "tier": "free"}
GUEST = {"sub": "guest", "name": "Guest", "tier": "free"}


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("SCORE_NODE_SECRET", SECRET)
    monkeypatch.delenv("CLI_ALLOWED_SUBS", raising=False)
    monkeypatch.setenv("GROQ_API_KEY", "x")
    monkeypatch.setenv("OPENROUTER_API_KEY", "x")
    node._links.clear()
    yield
    node._links.clear()


# ── 1. argv injection ────────────────────────────────────────────────────────

INJECTIONS = ["--settings={}", "--mcp-config=/tmp/x", "--add-dir=/", "--dangerously-skip-permissions"]


@pytest.mark.parametrize("style", ["claude_code", "codex", "opencode", "aider", "gemini_cli"])
@pytest.mark.parametrize("prompt", INJECTIONS)
def test_prompt_cannot_be_parsed_as_an_option(style, prompt):
    p = {"cli_style": style, "command": "tool"}
    a = llm._subprocess_argv(p, "m", "sys", prompt)
    if style in ("aider", "gemini_cli"):
        # flag-valued: the prompt is glued to its flag, never a separate token
        assert prompt not in a and any(t.endswith("=" + prompt) for t in a)
    else:
        assert a[-2:] == ["--", prompt]            # positional, after the end-of-options marker
    assert a.count(prompt) <= 1


def test_claude_code_prompt_after_double_dash():
    a = llm._subprocess_argv(llm.BUILTIN_PROVIDERS["claude_code"], "haiku", "sys", "--settings={}")
    assert a[-2:] == ["--", "--settings={}"]
    assert a.index("--") > a.index("--no-session-persistence")


def test_subprocess_stdin_devnull_and_scrubbed_env(monkeypatch):
    seen = {}

    class P:
        returncode = 0
        async def communicate(self):
            return b"ok", b""

    async def fake_exec(*argv, **kw):
        seen.update(kw)
        return P()
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setenv("JWT_SECRET", "s" * 32)
    monkeypatch.setenv("BEAM_DEVICE_TOKEN", "tok")
    monkeypatch.setenv("RENDER_API_KEY", "r")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "keep-for-claude-auth")
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/1000")

    async def run():
        return [c async for c in llm._stream_subprocess(
            {"provider": "claude_code", "model": "haiku"}, [{"role": "user", "content": "hi"}], "sys")]
    assert asyncio.run(run()) == ["ok"]
    assert seen["stdin"] == asyncio.subprocess.DEVNULL
    e = seen["env"]
    for dropped in ("JWT_SECRET", "SCORE_NODE_SECRET", "BEAM_DEVICE_TOKEN", "RENDER_API_KEY", "GROQ_API_KEY"):
        assert dropped not in e
    assert "PATH" in e and e["ANTHROPIC_API_KEY"] and e["XDG_RUNTIME_DIR"]


# ── 2/3. CLI providers are explicit-allow ────────────────────────────────────

def cli_in(candidates):
    return [c for c in candidates if llm._is_cli(c["provider"])]


def test_member_key_user_gets_no_cli_provider():
    for tier in llm.DEFAULT_MODEL_CHAINS:
        assert not cli_in(llm._resolve_candidates(tier, None, MEMBER))
    # the default backend's fast chain starts with claude_code: it must be stripped, not kept
    assert llm.DEFAULT_MODEL_CHAINS["fast"][0]["provider"] == "claude_code"
    # an explicit chain does not smuggle one in either
    with pytest.raises(RuntimeError):
        llm._resolve_candidates("fast", [{"provider": "codex", "model": "m"}], MEMBER)


def test_cli_allowed_subs_and_owner(monkeypatch):
    assert cli_in(llm._resolve_candidates("fast", None, OWNER))
    monkeypatch.setenv("CLI_ALLOWED_SUBS", "other, u-member-1")
    assert cli_in(llm._resolve_candidates("fast", None, MEMBER))
    assert not cli_in(llm._resolve_candidates("fast", None, {"sub": "u-nobody"}))
    assert not cli_in(llm._resolve_candidates("fast", None, GUEST))


def test_cli_detected_by_type_not_cli_style(monkeypatch):
    monkeypatch.setitem(llm.BUILTIN_PROVIDERS, "sneaky", {
        "type": "subprocess", "command": "x", "api_key_env": "", "api_key_required": False, "extra_headers": {}})
    assert llm._is_cli("sneaky")
    assert not llm._is_cli("groq")


def test_node_entry_serves_only_its_owner():
    chain = [{"provider": "node", "model": "sonnet", "owner": "pocket"}, {"provider": "groq", "model": "m"}]
    assert [c["provider"] for c in llm._resolve_candidates("fast", chain, OWNER)] == ["node", "groq"]
    assert [c["provider"] for c in llm._resolve_candidates("fast", chain, MEMBER)] == ["groq"]
    assert [c["provider"] for c in llm._resolve_candidates("fast", chain, None)] == ["groq"]


def test_beam_guest_and_member_get_no_cli(monkeypatch):
    import beam
    monkeypatch.setenv("BEAM_LLM_CHAIN", "claude_code:haiku,groq:openai/gpt-oss-20b")
    for user in (GUEST, MEMBER):
        s = beam.BeamSession(None, user)
        got = llm._resolve_candidates("fast", s.turn_chain(), user)
        assert got and not cli_in(got) and not any(c["provider"] == "node" for c in got)
    # the device identity keeps the chain as configured
    s = beam.BeamSession(None, {"sub": "pocket"})
    assert cli_in(llm._resolve_candidates("fast", s.turn_chain(), s.user))


def test_beam_passes_user_to_stream_chat(monkeypatch):
    import beam
    seen = []

    async def fake_stream_chat(*a, **kw):
        seen.append(kw.get("user"))
        yield '{"title": "t"}'
    monkeypatch.setattr(llm, "stream_chat", fake_stream_chat)
    s = beam.BeamSession(None, MEMBER)
    asyncio.run(s.shape("hello world", "voice"))
    assert seen and seen[0] is MEMBER
    assert open(beam.__file__).read().count("user=self.user") >= 2   # shaping and the voice turn


# ── 4. register is off by default ────────────────────────────────────────────

def test_register_disabled_unless_allowed(monkeypatch):
    import auth_routes
    import users as user_store
    app = FastAPI()
    app.include_router(auth_routes.router)
    c = TestClient(app)
    body = {"name": "Eve", "email": "eve@example.com"}
    monkeypatch.delenv("ALLOW_REGISTER", raising=False)
    assert c.post("/api/v1/auth/register", json=body).status_code == 404
    monkeypatch.setenv("ALLOW_REGISTER", "false")
    assert c.post("/api/v1/auth/register", json=body).status_code == 404
    created = []
    monkeypatch.setattr(user_store, "list_handles", lambda: [])
    monkeypatch.setattr(user_store, "create_user", lambda p: created.append(p))
    monkeypatch.setenv("ALLOW_REGISTER", "true")
    r = c.post("/api/v1/auth/register", json=body)
    assert r.status_code == 201 and created and r.json()["api_key"].startswith("rbbl_")


# ── 5. CORS ──────────────────────────────────────────────────────────────────

def test_cors_origins(monkeypatch):
    import auth
    import main
    monkeypatch.delenv("CORS_ORIGINS", raising=False)
    monkeypatch.delenv("FRONTEND_URL", raising=False)
    monkeypatch.setattr(auth, "DEMO_MODE_PUBLIC", False)
    assert main._cors_origins() == []                       # local: none by default
    monkeypatch.setenv("FRONTEND_URL", "*")
    assert main._cors_origins() == []                       # FRONTEND_URL=* does not open a local instance
    monkeypatch.setenv("CORS_ORIGINS", "http://localhost:3000, https://a.example")
    assert main._cors_origins() == ["http://localhost:3000", "https://a.example"]
    monkeypatch.delenv("CORS_ORIGINS")
    monkeypatch.setattr(auth, "DEMO_MODE_PUBLIC", True)     # cloud keeps today's behaviour
    assert main._cors_origins() == ["*"]
    monkeypatch.setenv("FRONTEND_URL", "https://w.example,https://x.example")
    assert main._cors_origins() == ["https://w.example", "https://x.example"]


def test_cors_never_credentials_with_wildcard():
    import main
    cors = [m for m in main.app.user_middleware if m.cls.__name__ == "CORSMiddleware"][0]
    origins = cors.kwargs["allow_origins"]
    assert not ("*" in origins and cors.kwargs["allow_credentials"])


# ── 6. node cwd + environment ────────────────────────────────────────────────

def test_context_dir_rules(monkeypatch, tmp_path):
    for k in ("CC_CONTEXT_DIR",):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("CC_WORKDIR", str(tmp_path))          # never a fallback
    with pytest.raises(RuntimeError, match="not set"):
        node_client.context_dir()
    monkeypatch.setenv("CC_CONTEXT_DIR", str(tmp_path / "missing"))
    with pytest.raises(RuntimeError, match="does not exist"):
        node_client.context_dir()
    ok = tmp_path / "ctx"
    ok.mkdir()
    monkeypatch.setenv("CC_CONTEXT_DIR", str(ok))
    assert node_client.context_dir() == os.path.realpath(ok)
    # inside a git work tree (a parent has .git): refused
    (tmp_path / ".git").mkdir()
    with pytest.raises(RuntimeError, match="git work tree"):
        node_client.context_dir()
    # a .git file (worktree/submodule) counts too, and so does a symlink into a repo
    (tmp_path / ".git").rmdir()
    (tmp_path / ".git").write_text("gitdir: /x")
    with pytest.raises(RuntimeError, match="git work tree"):
        node_client.context_dir()


def test_run_claude_refuses_without_context_dir(monkeypatch):
    monkeypatch.delenv("CC_CONTEXT_DIR", raising=False)
    monkeypatch.setenv("CC_WORKDIR", "/tmp")
    with pytest.raises(RuntimeError, match="CC_CONTEXT_DIR"):
        asyncio.run(node_client.run_claude([{"role": "user", "content": "x"}], "s", "haiku", 5))


def test_run_claude_env_is_scrubbed(monkeypatch, tmp_path):
    seen = {}

    class P:
        returncode = 0
        async def communicate(self):
            return b"ok", b""

    async def fake_exec(*argv, **kw):
        seen.update(kw)
        return P()
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setenv("CC_CONTEXT_DIR", str(tmp_path))
    monkeypatch.setenv("BEAM_DEVICE_TOKEN", "tok")
    asyncio.run(node_client.run_claude([{"role": "user", "content": "x"}], "s", "haiku", 5))
    assert "SCORE_NODE_SECRET" not in seen["env"] and "BEAM_DEVICE_TOKEN" not in seen["env"]
    assert "JWT_SECRET" not in seen["env"] and seen["stdin"] == asyncio.subprocess.DEVNULL


# ── 7. deadline and cancel ───────────────────────────────────────────────────

def test_claude_timeout_is_deadline_minus_three(monkeypatch, tmp_path):
    got = []

    async def fake_run(messages, system, model, timeout):
        got.append(timeout)
        return "ok"

    class WS:
        async def send(self, data):
            pass
    monkeypatch.setattr(node_client, "run_claude", fake_run)
    monkeypatch.setenv("BEAM_NODE_DEADLINE_S", "25")

    async def go(deadline):
        await node_client.handle_turn(WS(), {"id": "a", "messages": [{"role": "user", "content": "x"}],
                                              "deadline_s": deadline}, asyncio.Lock())
    asyncio.run(go(25))
    asyncio.run(go(6))
    assert got == [22, 5]                                     # min 5


def test_run_claude_kills_process_on_cancel(monkeypatch, tmp_path):
    state = {}

    class P:
        returncode = None
        def kill(self):
            state["killed"] = True
        async def wait(self):
            return -9
        async def communicate(self):
            await asyncio.sleep(60)

    async def fake_exec(*argv, **kw):
        return P()
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setenv("CC_CONTEXT_DIR", str(tmp_path))

    async def go():
        t = asyncio.create_task(node_client.run_claude([{"role": "user", "content": "x"}], "s", "haiku", 30))
        await asyncio.sleep(0.05)
        t.cancel()
        with pytest.raises(asyncio.CancelledError):
            await t
    asyncio.run(go())
    assert state.get("killed")


def test_client_cancel_frame_kills_turn_and_acks(monkeypatch, tmp_path):
    """The websocket loop in node_client.session: turn then turn.cancel -> run dies, ack sent."""
    state = {"sent": []}
    started = asyncio.Event

    async def fake_run(messages, system, model, timeout):
        state["running"] = True
        try:
            await asyncio.sleep(60)
        finally:
            state["stopped"] = True

    class WS:
        def __init__(self):
            self.q = asyncio.Queue()
        async def send(self, data):
            if '"type": "node.hello"' in data:
                self.q.put_nowait(json.dumps({"type": "node.ready"}))
            else:
                state["sent"].append(json.loads(data))
        async def recv(self):
            return await self.q.get()
        def __aiter__(self):
            return self
        async def __anext__(self):
            item = await self.q.get()
            if item is None:
                raise StopAsyncIteration
            return item

    class Conn:
        def __init__(self, ws):
            self.ws = ws
        async def __aenter__(self):
            return self.ws
        async def __aexit__(self, *a):
            return False

    ws = WS()
    monkeypatch.setattr(node_client, "run_claude", fake_run)
    monkeypatch.setattr(node_client.websockets, "connect", lambda *a, **k: Conn(ws))

    async def go():
        t = asyncio.create_task(node_client.session("wss://x/y", SECRET, "n"))
        await asyncio.sleep(0.05)
        ws.q.put_nowait(json.dumps({"type": "turn", "id": "t1", "messages": [{"role": "user", "content": "x"}]}))
        await asyncio.sleep(0.1)
        assert state.get("running")
        ws.q.put_nowait(json.dumps({"type": "turn.cancel", "id": "t1"}))
        await asyncio.sleep(0.1)
        ws.q.put_nowait(b"\xff\xfe binary junk")             # does not crash the loop
        ws.q.put_nowait(None)
        await asyncio.wait_for(t, 2)
    asyncio.run(go())
    assert state.get("stopped")
    assert {"type": "turn.error", "id": "t1", "code": "cancelled", "msg": "cancelled"} in state["sent"]


@pytest.fixture
def app_client(monkeypatch):
    app = FastAPI()
    app.include_router(node.router)
    chain = [{"provider": "node", "model": "sonnet", "owner": "pocket", "device_id": "d"},
             {"provider": "claude_code", "model": "m"}]

    @app.get("/run")
    async def run():
        try:
            return {"text": "".join([c async for c in llm.stream_chat(
                [{"role": "user", "content": "hi"}], "sys", model_tier="fast", chain=chain, user=OWNER)])}
        except Exception as exc:
            return {"error": str(exc)}

    async def fallback(candidate, messages, system):
        yield "from the chain"
    monkeypatch.setattr(llm, "_stream_subprocess", fallback)
    with TestClient(app) as c:
        yield c


def start_run(client):
    import concurrent.futures
    return concurrent.futures.ThreadPoolExecutor(1).submit(lambda: client.get("/run").json())


def connect(client, secret=SECRET):
    return client.websocket_connect("/api/v1/node", headers={"Authorization": f"Bearer {secret}"})


def link_up(ws):
    ws.send_text(json.dumps(HELLO))
    assert ws.receive_json() == {"type": "node.ready", "owner": "pocket"}


def wait_for(cond, secs=3.0):
    end = time.monotonic() + secs
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


def test_cloud_sends_cancel_on_timeout_and_stays_busy_until_ack(app_client, monkeypatch):
    monkeypatch.setenv("BEAM_NODE_DEADLINE_S", "0.2")
    with connect(app_client) as ws:
        link_up(ws)
        fut = start_run(app_client)
        turn = ws.receive_json()
        assert turn["type"] == "turn"
        assert fut.result(5) == {"text": "from the chain"}      # fell back
        cancel = ws.receive_json()
        assert cancel == {"type": "turn.cancel", "id": turn["id"]}
        assert node._links["pocket"].busy                       # not free until the node acks
        assert node.node_route("pocket") == []
        ws.send_json({"type": "turn.error", "id": turn["id"], "code": "cancelled", "msg": "cancelled"})
        assert wait_for(lambda: not node._links["pocket"].busy)
        node._links["pocket"].reset()
        assert node.node_route("pocket")


def test_busy_cleared_on_link_drop(app_client, monkeypatch):
    monkeypatch.setenv("BEAM_NODE_DEADLINE_S", "0.2")
    with connect(app_client) as ws:
        link_up(ws)
        fut = start_run(app_client)
        ws.receive_json()
        fut.result(5)
        link = node._links["pocket"]
        assert link.busy
    assert wait_for(lambda: not link.busy)                      # the drop frees it


def test_replacing_a_node_link_mid_turn_falls_back(app_client):
    with connect(app_client) as ws1:
        link_up(ws1)
        fut = start_run(app_client)
        assert ws1.receive_json()["type"] == "turn"
        with connect(app_client) as ws2:
            link_up(ws2)                                         # newer node replaces the old one
            assert fut.result(5) == {"text": "from the chain"}
            assert node._links["pocket"].ws is not None
            assert node.node_route("pocket")                     # the new link is idle and routable


# ── 8. secrets, transport, bad frames ────────────────────────────────────────

def test_short_node_secret_refused_on_both_sides(monkeypatch):
    monkeypatch.setenv("SCORE_NODE_SECRET", "short-secret")
    assert node.owner_for_secret("short-secret") is None
    monkeypatch.setenv("SCORE_NODE_SECRET", SECRET)
    assert node.owner_for_secret(SECRET) == "pocket"
    with pytest.raises(RuntimeError):
        node_client.check_secret("short-secret")
    assert node_client.check_secret(SECRET) == SECRET


def test_client_refuses_cleartext_remote_url():
    for ok in ("wss://score.joinrabble.world/api/v1/node", "ws://127.0.0.1:8083/api/v1/node",
               "ws://localhost:8083/x", "ws://[::1]:8083/x"):
        assert node_client.check_url(ok) == ok
    for bad in ("ws://score.joinrabble.world/api/v1/node", "ws://192.168.1.5:8083/x",
                "http://127.0.0.1/x", "ws://127.0.0.1.evil.example/x", ""):
        with pytest.raises(RuntimeError):
            node_client.check_url(bad)


def test_cloud_survives_binary_and_garbage_frames():
    app = FastAPI()
    app.include_router(node.router)
    with TestClient(app) as c, connect(c) as ws:
        link_up(ws)
        ws.send_bytes(b"\x00\xff\xfe")
        ws.send_text("not json")
        ws.send_text("[1,2,3]")
        ws.send_text(json.dumps({"type": "turn.reply", "id": 5, "text": {"x": 1}}))
        ws.send_text(json.dumps({"type": "keepalive"}))
        assert ws.receive_json() == {"type": "keepalive"}       # still alive and answering


def test_cloud_binary_hello_closes_without_crash():
    app = FastAPI()
    app.include_router(node.router)
    with TestClient(app) as c, connect(c) as ws:
        ws.send_bytes(b"\x00\xff")
        with pytest.raises(Exception):
            ws.receive_json()
