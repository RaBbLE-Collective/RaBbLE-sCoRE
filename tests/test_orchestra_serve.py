"""The engine server (orchestra/serve.py): handshake, sessions, fan-out, ACL, reattach.
Real Unix sockets, no model calls (recorded fixtures + a fake backend).
  ~/.local/share/RaBbLE/rabble-ui/bin/python -m pytest tests/test_orchestra_serve.py"""

import asyncio
import json
import os
import stat
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from orchestra import events as ev  # noqa: E402
from orchestra.backend import _Queued  # noqa: E402
from orchestra.serve import LINE_LIMIT, Principal, Server, SessionManager, UnixListener, replay_factory  # noqa: E402

FIXTURE = str(Path(__file__).parent / "fixtures" / "orchestra" / "tool-turn.jsonl")
pytestmark = pytest.mark.asyncio


class FakeBackend(_Queued):
    """Emits an ask on send; the first answer wins, like the real Permissions."""

    def __init__(self):
        super().__init__()
        self.answered: list = []
        self.sent: list = []

    async def start(self):
        self.emit(ev.SessionStarted("sdk-1", "haiku", "/w"))

    async def send(self, text):
        self.sent.append(text)
        self.emit(ev.PermissionAsk("a1", "rabble", "Bash", {"command": "ls"}))

    def answer(self, ask_id, reply):
        if self.answered:
            return False
        self.answered.append((ask_id, reply))
        return True

    async def interrupt(self): ...
    async def set_model(self, model): ...
    async def set_permission_mode(self, mode): ...
    def set_trust(self, profile): ...
    async def stop_task(self, task_id): ...

    async def close(self):
        self._finish()


class Client:
    def __init__(self, reader, writer):
        self.r, self.w = reader, writer

    def send(self, **msg):
        self.w.write(json.dumps(msg).encode() + b"\n")

    async def get(self, timeout=2.0):
        return json.loads(await asyncio.wait_for(self.r.readline(), timeout))

    async def until(self, type_, timeout=2.0):
        while True:
            m = await self.get(timeout)
            if m["type"] == type_:
                return m

    async def hello(self, name="test"):
        self.send(type="hello", version=1, client=name)
        return await self.until("hello")


class Rig:
    """A server on a temp socket; each connection's principal is chosen by the test, as a transport would."""

    def __init__(self, tmp_path, factory):
        self.path = str(tmp_path / "e.sock")
        self.server = Server(SessionManager(factory), "test-engine")
        self.principals = ["local:1"]
        self.n = 0
        self.clients: list[Client] = []

    async def __aenter__(self):
        async def accept(reader, writer):
            p = Principal(self.principals[min(self.n, len(self.principals) - 1)])
            self.n += 1
            await self.server.handle(reader, writer, p)
        self.srv = await asyncio.start_unix_server(accept, self.path, limit=LINE_LIMIT)
        return self

    async def __aexit__(self, *a):
        for c in self.clients:                   # wait_closed() waits for every open connection (3.12+)
            c.w.close()
        self.srv.close()
        await asyncio.wait_for(self.srv.wait_closed(), 3)

    async def client(self):
        r, w = await asyncio.open_unix_connection(self.path, limit=LINE_LIMIT)
        self.clients.append(Client(r, w))
        return self.clients[-1]


async def fake_factory(_p):
    return FakeBackend()


async def test_hello_is_required_and_versioned(tmp_path):
    async with Rig(tmp_path, fake_factory) as rig:
        c = await rig.client()
        c.send(type="list_sessions")
        assert (await c.get())["text"] == "say hello first"
        c.send(type="hello", version=99)
        assert "not supported" in (await c.until("Error"))["text"]
        c2 = await rig.client()
        assert (await c2.hello())["user"] == "local:1"


async def test_bad_input_is_an_error_not_a_crash(tmp_path):
    async with Rig(tmp_path, fake_factory) as rig:
        c = await rig.client()
        await c.hello()
        c.w.write(b"{not json\n")
        c.send(type="frobnicate")
        c.send(type="send")                      # no session
        assert (await c.until("Error"))["type"] == "Error"
        assert "unknown command" in (await c.until("Error"))["text"]
        assert "no such session" in (await c.until("Error"))["text"]
        c.send(type="list_sessions")             # still alive
        assert (await c.until("sessions"))["sessions"] == []


async def test_replayed_turn_reaches_two_clients_with_session_addressing(tmp_path):
    async with Rig(tmp_path, replay_factory(FIXTURE)) as rig:
        a, b = await rig.client(), await rig.client()
        await a.hello("tui")
        await b.hello("gui")
        a.send(type="create_session", cwd="/w")
        sid = (await a.until("session_created"))["session"]
        b.send(type="attach", session=sid)
        assert (await b.until("attached"))["role"] == "owner"       # same principal, so same owner
        a.send(type="send", session=sid, text="count the files")
        for c in (a, b):
            seen = []
            while True:
                m = await c.get()
                assert m.get("session", sid) == sid
                seen.append(m["type"])
                if m["type"] == "TurnDone":
                    break
            assert "UserSaid" in seen and "ToolCall" in seen and "TextDelta" in seen


async def test_first_answer_wins_and_the_others_are_told(tmp_path):
    async with Rig(tmp_path, fake_factory) as rig:
        a, b = await rig.client(), await rig.client()
        await a.hello()
        await b.hello()
        a.send(type="create_session")
        sid = (await a.until("session_created"))["session"]
        b.send(type="attach", session=sid)
        await b.until("attached")
        a.send(type="send", session=sid, text="run it")
        assert (await a.until("PermissionAsk"))["id"] == "a1"
        assert (await b.until("PermissionAsk"))["id"] == "a1"
        a.send(type="answer", session=sid, ask_id="a1", decision="allow")
        b.send(type="answer", session=sid, ask_id="a1", decision="deny")
        r = await b.until("PermissionResolved")
        assert (r["id"], r["decision"], r["by"]) == ("a1", "allow", "local:1")
        assert "already resolved" in (await b.until("Error"))["text"]


async def test_late_attach_catches_up_with_open_asks(tmp_path):
    async with Rig(tmp_path, fake_factory) as rig:
        a, b = await rig.client(), await rig.client()
        await a.hello()
        await b.hello()
        a.send(type="create_session")
        sid = (await a.until("session_created"))["session"]
        a.send(type="send", session=sid, text="run it")
        await a.until("PermissionAsk")
        b.send(type="attach", session=sid)
        await b.until("attached")
        assert (await b.until("SessionStarted"))["model"] == "haiku"
        assert (await b.until("PermissionAsk"))["id"] == "a1"


async def test_session_outlives_its_client(tmp_path):
    async with Rig(tmp_path, fake_factory) as rig:
        a = await rig.client()
        await a.hello()
        a.send(type="create_session", cwd="/keep")
        sid = (await a.until("session_created"))["session"]
        a.w.close()
        await asyncio.sleep(0.1)
        b = await rig.client()
        await b.hello()
        b.send(type="list_sessions")
        assert [(s["session"], s["clients"], s["cwd"]) for s in (await b.until("sessions"))["sessions"]] == [(sid, 0, "/keep")]
        b.send(type="attach", session=sid)
        assert (await b.until("attached"))["role"] == "owner"


async def test_a_stranger_sees_nothing_until_granted(tmp_path):
    async with Rig(tmp_path, fake_factory) as rig:
        rig.principals = ["local:1", "local:2", "local:2"]
        owner, guest = await rig.client(), await rig.client()
        await owner.hello()
        await guest.hello()
        owner.send(type="create_session")
        sid = (await owner.until("session_created"))["session"]
        guest.send(type="list_sessions")
        assert (await guest.until("sessions"))["sessions"] == []
        guest.send(type="attach", session=sid)
        assert "not allowed" in (await guest.until("Error"))["text"]
        guest.send(type="send", session=sid, text="hi")
        assert "not allowed" in (await guest.until("Error"))["text"]
        owner.send(type="grant", session=sid, principal="local:2", role="observer")
        await asyncio.sleep(0.05)
        guest.send(type="attach", session=sid)
        assert (await guest.until("attached"))["role"] == "observer"
        guest.send(type="send", session=sid, text="hi")                  # observers watch, they do not drive
        assert "not allowed" in (await guest.until("Error"))["text"]
        guest.send(type="close_session", session=sid)                    # and only the owner closes
        assert "not allowed" in (await guest.until("Error"))["text"]


async def test_close_session_tells_every_client(tmp_path):
    async with Rig(tmp_path, fake_factory) as rig:
        a = await rig.client()
        await a.hello()
        a.send(type="create_session")
        sid = (await a.until("session_created"))["session"]
        a.send(type="close_session", session=sid)
        assert (await a.until("session_closed"))["session"] == sid
        a.send(type="list_sessions")
        assert (await a.until("sessions"))["sessions"] == []


async def test_unix_listener_is_private_and_identifies_the_os_user(tmp_path):
    path = tmp_path / "run" / "engine.sock"
    listener = UnixListener(path)
    server = Server(SessionManager(fake_factory), "t")
    await listener.start(server.handle)
    try:
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        r, w = await asyncio.open_unix_connection(str(path))
        c = Client(r, w)
        assert (await c.hello())["user"] == f"local:{os.getuid()}"
        with pytest.raises(RuntimeError, match="already listening"):
            await UnixListener(path).start(server.handle)
    finally:
        w.close()
        await asyncio.wait_for(listener.close(), 3)
    assert not path.exists()
