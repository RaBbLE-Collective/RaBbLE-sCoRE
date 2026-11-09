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
from orchestra import serve as serve_mod  # noqa: E402
from orchestra.serve import LINE_LIMIT, UP_LINE_LIMIT, Principal, Server, SessionManager, UnixListener, replay_factory  # noqa: E402

FIXTURE = str(Path(__file__).parent / "fixtures" / "orchestra" / "tool-turn.jsonl")
pytestmark = pytest.mark.asyncio


class FakeBackend(_Queued):
    """Emits an ask on send; the first answer wins, like the real Permissions."""

    def __init__(self):
        super().__init__()
        self.answered: list = []
        self.sent: list = []
        self.calls: list = []
        self.closed = False

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
    async def set_model(self, model):
        self.calls.append(("set_model", model))

    async def set_permission_mode(self, mode):
        self.calls.append(("set_permission_mode", mode))

    def set_trust(self, profile):
        self.calls.append(("set_trust", profile))

    async def stop_task(self, task_id): ...

    async def close(self):
        self.closed = True
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
        # the rig's principals are made-up users, so the policy stub (host user only) is swapped for allow-all
        self.server = Server(SessionManager(factory), "test-engine", policy=lambda p, c, params: params)
        self.principals = ["local:1"]
        self.n = 0
        self.clients: list[Client] = []

    async def __aenter__(self):
        async def accept(reader, writer):
            p = Principal(self.principals[min(self.n, len(self.principals) - 1)])
            self.n += 1
            await self.server.handle(reader, writer, p)
        self.srv = await asyncio.start_unix_server(accept, self.path, limit=UP_LINE_LIMIT)
        return self

    async def __aexit__(self, *a):
        for c in self.clients:                   # wait_closed() waits for every open connection (3.12+)
            c.w.close()
        self.srv.close()
        await asyncio.wait_for(self.srv.wait_closed(), 3)

    async def client(self, limit=LINE_LIMIT):
        r, w = await asyncio.open_unix_connection(self.path, limit=limit)
        self.clients.append(Client(r, w))
        return self.clients[-1]


async def fake_factory(_p):
    return FakeBackend()


async def test_hello_is_required_and_versioned(tmp_path):
    async with Rig(tmp_path, fake_factory) as rig:
        c = await rig.client()
        c.send(type="list_sessions")
        assert (await c.get())["text"] == "say hello first"
        c.send(type="hello", version=0)
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


class SelfEndingBackend(FakeBackend):
    """Answers a send with a finished turn, having called end_session."""

    end_requested = False

    async def send(self, text):
        self.sent.append(text)
        self.end_requested = True
        self.emit(ev.TurnDone("end_turn"))


async def test_a_session_that_ends_itself_closes_after_its_turn(tmp_path):
    backend = SelfEndingBackend()

    async def factory(_p):
        return backend
    async with Rig(tmp_path, factory) as rig:
        a = await rig.client()
        await a.hello()
        a.send(type="create_session", cwd="/done")
        sid = (await a.until("session_created"))["session"]
        a.send(type="send", session=sid, text="do the thing")
        await a.until("TurnDone")                              # the turn is delivered first
        assert (await a.until("session_closed"))["session"] == sid
        assert backend.closed and sid not in rig.server.manager.sessions


async def test_idle_detached_session_is_reaped_but_attached_or_busy_is_kept(tmp_path):
    async with Rig(tmp_path, fake_factory) as rig:
        a = await rig.client()
        await a.hello()
        a.send(type="create_session", cwd="/gone")
        gone = (await a.until("session_created"))["session"]
        a.send(type="create_session", cwd="/kept")
        kept = (await a.until("session_created"))["session"]
        mgr = rig.server.manager
        a.send(type="detach", session=gone)
        await a.until("detached")
        assert await mgr.reap_idle(ttl=3600) == []             # not idle long enough
        await asyncio.sleep(0.05)
        assert await mgr.reap_idle(ttl=0.01) == [gone]         # detached and quiet: closed
        assert set(mgr.sessions) == {kept}                     # still attached: left alone
        mgr.sessions[kept].clients.clear()
        mgr.sessions[kept].busy = True
        assert await mgr.reap_idle(ttl=0.01) == []             # a running turn is not idle


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
        stranger = (await guest.until("Error"))["text"]
        guest.send(type="attach", session="s-nonexistent")
        absent = (await guest.until("Error"))["text"]
        assert stranger.startswith("no such session") and absent.startswith("no such session")   # no oracle for existence
        assert stranger.replace(repr(sid), "X") == absent.replace("'s-nonexistent'", "X")
        guest.send(type="send", session=sid, text="hi")
        assert "no such session" in (await guest.until("Error"))["text"]
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


# ── protocol v1.1 + hardening ───────────────────────────────────────────────

class TalkyBackend(FakeBackend):
    """send() streams a few deltas, then waits on an ask: a turn in flight."""

    async def send(self, text):
        self.sent.append(text)
        for t in ("one ", "two ", "three"):
            self.emit(ev.TextDelta("rabble", t))
        self.emit(ev.PermissionAsk("a1", "rabble", "Bash", {"command": "ls"}))


async def talky(_p):
    return TalkyBackend()


async def make_session(rig, **params):
    a = await rig.client()
    await a.hello("tui")
    a.send(type="create_session", **params)
    sid = (await a.until("session_created"))["session"]
    return a, sid


async def test_hello_advertises_features_and_accepts_newer_clients(tmp_path):
    async with Rig(tmp_path, fake_factory) as rig:
        c = await rig.client()
        c.send(type="hello", version=7, client="future", id="h1")
        h = await c.until("hello")
        assert h["id"] == "h1" and h["version"] == 1
        assert {"seq", "attach_since", "request_id", "snapshot", "owner_settings"} <= set(h["features"])


async def test_replies_echo_the_request_id_and_events_do_not(tmp_path):
    async with Rig(tmp_path, talky) as rig:
        a, sid = await make_session(rig)
        a.send(type="list_sessions", id="r1")
        assert (await a.until("sessions"))["id"] == "r1"
        a.send(type="frobnicate", id="r2")
        assert (await a.until("Error"))["id"] == "r2"
        a.send(type="attach", session=sid, id="r3")
        assert (await a.until("attached"))["id"] == "r3"
        a.send(type="send", session=sid, text="go", id="r4")
        said = await a.until("UserSaid")
        assert "id" not in said and said["client"] == "tui" and said["sender"] == "local:1"
        # an event's own body id is the ask's, never the request's
        ask = await a.until("PermissionAsk")
        assert ask["id"] == "a1"


async def test_late_attach_mid_turn_gets_the_transcript_tail_and_a_snapshot(tmp_path):
    async with Rig(tmp_path, talky) as rig:
        a, sid = await make_session(rig)
        a.send(type="send", session=sid, text="go")
        await a.until("PermissionAsk")
        b = await rig.client()
        await b.hello("gui")
        b.send(type="attach", session=sid)
        att = await b.until("attached")
        assert att["gap"] is False and att["seq"] >= 6
        snap = att["snapshot"]
        assert snap["busy"] is True and snap["turn_owner"] == "local:1"
        assert snap["model"] == "haiku" and snap["sdk_session_id"] == "sdk-1"
        got = []
        while True:
            m = await b.get()
            got.append(m)
            if m["type"] == "PermissionAsk":
                break
        assert [m["text"] for m in got if m["type"] == "TextDelta"] == ["one ", "two ", "three"]
        assert [m["seq"] for m in got] == list(range(1, len(got) + 1))     # contiguous, from the start
        assert att["seq"] == got[-1]["seq"]
        # a client that already saw up to seq 4 asks for the rest only
        c = await rig.client()
        await c.hello()
        c.send(type="attach", session=sid, since_seq=4)
        await c.until("attached")
        first = await c.get()
        assert first["seq"] == 5


async def test_ring_overflow_sets_gap_and_sends_compact_state(tmp_path):
    async with Rig(tmp_path, talky) as rig:
        a, sid = await make_session(rig)
        rig.server.manager.sessions[sid].ring_events = 3
        a.send(type="send", session=sid, text="go")
        await a.until("PermissionAsk")
        b = await rig.client()
        await b.hello()
        b.send(type="attach", session=sid, since_seq=1)
        att = await b.until("attached")
        assert att["gap"] is True
        seen = []
        while True:
            m = await b.get()
            seen.append(m)
            if m["type"] == "PermissionAsk" and "seq" in m:
                break
        kinds = [m["type"] for m in seen]
        assert kinds[0] == "SessionStarted" and "seq" not in seen[0]       # compact state first, unsequenced
        tail = [m for m in seen if "seq" in m]
        assert [m["seq"] for m in tail] == [att["seq"] - 2, att["seq"] - 1, att["seq"]]     # exactly the retained 3
        c = await rig.client()                                              # at the head: no gap
        await c.hello()
        c.send(type="attach", session=sid, since_seq=att["seq"])
        assert (await c.until("attached"))["gap"] is False


async def test_a_stalled_client_is_disconnected_and_can_reattach(tmp_path, monkeypatch):
    class Flood(FakeBackend):
        async def send(self, text):
            async def go():
                for i in range(60):
                    self.emit(ev.TextBlock("rabble", f"{i:02d}" + "x" * 50_000))
                    await asyncio.sleep(0.002)                # a healthy reader keeps up with this
                self.emit(ev.TurnDone("end_turn"))
            self._t = asyncio.create_task(go())

    async def flood(_p):
        return Flood()

    monkeypatch.setattr(serve_mod, "MAX_QUEUE_BYTES", 300_000)
    async with Rig(tmp_path, flood) as rig:
        rig.principals = ["local:1"]
        a, sid = await make_session(rig)                     # a reads; b never will
        b = await rig.client(limit=1024)                      # a tiny read buffer: asyncio stops pulling from the socket
        await b.hello()
        b.send(type="attach", session=sid)
        await b.until("attached")
        a.send(type="send", session=sid, text="go")
        await a.until("TurnDone", timeout=5)                 # the engine kept going for the healthy client
        # b's socket buffer filled, its queue passed the cap: it was cut off, with a notice
        assert len(rig.server.manager.sessions[sid].clients) == 1
        data = b""
        while chunk := await asyncio.wait_for(b.r.read(1 << 20), 5):
            data += chunk
        assert b"client too slow" in data
        # reattach from near the head: no gap, the rest arrives (a full-ring replay would itself exceed this tiny cap)
        c = await rig.client()
        await c.hello()
        c.send(type="attach", session=sid, since_seq=rig.server.manager.sessions[sid].seq - 2)
        assert (await c.until("attached"))["gap"] is False
        assert (await c.until("TurnDone", timeout=5))["type"] == "TurnDone"


async def test_non_owner_cannot_change_settings_but_can_drive(tmp_path):
    async with Rig(tmp_path, talky) as rig:
        rig.principals = ["local:1", "local:2"]
        owner, sid = await make_session(rig)
        guest = await rig.client()
        await guest.hello()
        owner.send(type="grant", session=sid, principal="local:2", role="participant")
        await asyncio.sleep(0.05)
        guest.send(type="attach", session=sid)
        await guest.until("attached")
        backend = rig.server.manager.sessions[sid].backend
        for cmd in ({"type": "set_model", "model": "opus"}, {"type": "set_permission_mode", "mode": "bypassPermissions"},
                    {"type": "set_trust", "profile": "trusted"}):
            guest.send(session=sid, **cmd)
            assert "not allowed" in (await guest.until("Error"))["text"]
        assert backend.calls == []
        guest.send(type="send", session=sid, text="drive")                # a participant may still drive
        await guest.until("UserSaid")
        owner.send(type="set_model", session=sid, model="opus")
        owner.send(type="set_trust", session=sid, profile="strict")
        owner.send(type="list_sessions", id="sync")
        await owner.until("sessions")
        assert backend.calls == [("set_model", "opus"), ("set_trust", "strict")]
        owner.send(type="attach", session=sid)
        assert (await owner.until("attached"))["snapshot"]["trust"] == "strict"


async def test_create_policy_can_rewrite_or_refuse_and_default_refuses_non_local(tmp_path):
    seen = []

    def policy(principal, command, params):
        seen.append((principal.id, command, dict(params)))
        if params.get("trust") == "trusted":
            return None
        return {**params, "cwd": "/sandbox"}

    async with Rig(tmp_path, fake_factory) as rig:
        rig.server.policy = policy
        a = await rig.client()
        await a.hello()
        a.send(type="create_session", trust="trusted", cwd="/etc", id="p1")
        e = await a.until("Error")
        assert e["id"] == "p1" and "not allowed" in e["text"]
        a.send(type="create_session", cwd="/etc", evil="x")
        sid = (await a.until("session_created"))["session"]
        a.send(type="list_sessions")
        assert (await a.until("sessions"))["sessions"][0]["cwd"] == "/sandbox"
        assert seen[1][2] == {"cwd": "/etc"}                              # unknown keys never reach the policy
    from orchestra.serve import local_policy
    assert local_policy(Principal("web:9", "web"), "create_session", {}) is None
    assert local_policy(Principal("local:0" if os.getuid() else "local:1"), "create_session", {}) is None   # another OS user
    me = Principal(f"local:{os.getuid()}")
    assert local_policy(me, "create_session", {"cwd": "/w"}) == {"cwd": "/w"}
    assert sid


async def test_failed_start_leaves_no_session(tmp_path):
    made = []

    class Broken(FakeBackend):
        async def start(self):
            raise RuntimeError("cli would not start: secret detail")

    async def factory(_p):
        made.append(Broken())
        return made[-1]

    async with Rig(tmp_path, factory) as rig:
        a = await rig.client()
        await a.hello()
        a.send(type="create_session", id="c1")
        e = await a.until("Error")
        assert e["id"] == "c1" and e["text"] == "could not start the session" and "secret" not in e["text"]
        assert made[0].closed is True
        a.send(type="list_sessions")                                        # the connection survived
        assert (await a.until("sessions"))["sessions"] == []
        assert rig.server.manager.sessions == {}


async def test_engine_fault_in_a_command_does_not_kill_the_connection(tmp_path):
    class Hurt(FakeBackend):
        async def set_model(self, model):
            raise OSError("boom")

    async def factory(_p):
        return Hurt()

    async with Rig(tmp_path, factory) as rig:
        a, sid = await make_session(rig)
        a.send(type="set_model", session=sid, model="x", id="m1")
        e = await a.until("Error")
        assert e["text"] == "internal error" and e["id"] == "m1"
        a.send(type="list_sessions")
        assert len((await a.until("sessions"))["sessions"]) == 1


async def test_two_engines_racing_the_socket_one_wins(tmp_path):
    path = tmp_path / "run" / "engine.sock"
    server = Server(SessionManager(fake_factory), "t")
    a, b = UnixListener(path), UnixListener(path)
    results = await asyncio.gather(a.start(server.handle), b.start(server.handle), return_exceptions=True)
    assert sum(isinstance(r, RuntimeError) for r in results) == 1
    winner, loser = (a, b) if results[0] is None else (b, a)
    try:
        await loser.close()                                                 # must not unlink the winner's socket
        r, w = await asyncio.open_unix_connection(str(path))
        assert (await Client(r, w).hello())["type"] == "hello"
        w.close()
        assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    finally:
        await asyncio.wait_for(winner.close(), 3)
    assert not path.exists()
    again = UnixListener(path)                                              # the lock went with the winner
    await again.start(server.handle)
    await again.close()


async def test_stale_socket_is_replaced_but_foreign_dir_is_refused(tmp_path):
    path = tmp_path / "engine.sock"
    path.write_text("")                                                     # a dead engine's leftover
    server = Server(SessionManager(fake_factory), "t")
    live = UnixListener(path)
    await live.start(server.handle)
    await live.close()
    assert not path.exists()
    if os.getuid() != 0:
        with pytest.raises(RuntimeError, match="not owned"):
            await UnixListener(Path("/usr/share/engine.sock")).start(server.handle)


async def test_bad_decision_is_rejected_and_the_ask_stays_open(tmp_path):
    async with Rig(tmp_path, talky) as rig:
        a, sid = await make_session(rig)
        a.send(type="send", session=sid, text="go")
        await a.until("PermissionAsk")
        for bad in ("maybe", None, 3):
            a.send(type="answer", session=sid, ask_id="a1", decision=bad, id="d1")
            e = await a.until("Error")
            assert "decision must be" in e["text"] and e["id"] == "d1"
        assert rig.server.manager.sessions[sid].backend.answered == []
        a.send(type="answer", session=sid, ask_id="a1", decision="allow_session")
        assert (await a.until("PermissionResolved"))["decision"] == "allow_session"


async def test_interrupt_resolves_open_asks_for_everyone(tmp_path):
    async with Rig(tmp_path, talky) as rig:
        a, sid = await make_session(rig)
        b = await rig.client()
        await b.hello()
        b.send(type="attach", session=sid)
        await b.until("attached")
        a.send(type="send", session=sid, text="go")
        await b.until("PermissionAsk")
        a.send(type="interrupt", session=sid)
        r = await b.until("PermissionResolved")
        assert (r["id"], r["decision"], r["by"]) == ("a1", "deny", "local:1")
        assert rig.server.manager.sessions[sid].asks == {}


async def test_turn_done_drops_asks_the_backend_no_longer_has_pending(tmp_path):
    class Pending:
        pending: list = []

    class WithPerms(TalkyBackend):
        def __init__(self):
            super().__init__()
            self.permissions = Pending()

    async def factory(_p):
        return WithPerms()

    async with Rig(tmp_path, factory) as rig:
        a, sid = await make_session(rig)
        backend = rig.server.manager.sessions[sid].backend
        a.send(type="send", session=sid, text="go")
        await a.until("PermissionAsk")
        backend.permissions.pending = ["a1"]                                 # a background organ's turn ends: the ask lives
        backend.emit(ev.TurnDone("end_turn", "background"))
        await a.until("TurnDone")
        assert "a1" in rig.server.manager.sessions[sid].asks
        backend.permissions.pending = []
        backend.emit(ev.TurnDone("end_turn"))
        await a.until("TurnDone")
        assert (await a.until("PermissionResolved"))["by"] == "engine"


async def test_usage_replay_merges_fields_instead_of_overwriting(tmp_path):
    async with Rig(tmp_path, fake_factory) as rig:
        a, sid = await make_session(rig)
        backend = rig.server.manager.sessions[sid].backend
        backend.emit(ev.Usage(100, 0.5, None))
        backend.emit(ev.Usage(120, None, 33.0))
        await asyncio.sleep(0.05)
        u = rig.server.manager.sessions[sid].usage
        assert (u.tokens, u.cost, u.context_pct) == (120, 0.5, 33.0)


async def test_revoke_removes_access_and_detaches(tmp_path):
    async with Rig(tmp_path, fake_factory) as rig:
        rig.principals = ["local:1", "local:2"]
        owner, sid = await make_session(rig)
        guest = await rig.client()
        await guest.hello()
        owner.send(type="grant", session=sid, principal="local:2", role="participant")
        await asyncio.sleep(0.05)
        guest.send(type="attach", session=sid)
        await guest.until("attached")
        owner.send(type="revoke", session=sid, principal="local:2")
        await guest.until("detached")
        guest.send(type="attach", session=sid)
        assert "no such session" in (await guest.until("Error"))["text"]


async def test_session_ids_are_random_and_listing_shows_the_sdk_id(tmp_path):
    async with Rig(tmp_path, fake_factory) as rig:
        a, sid = await make_session(rig)
        assert sid.startswith("s") and str(os.getpid()) not in sid
        await asyncio.sleep(0.05)
        a.send(type="list_sessions")
        row = (await a.until("sessions"))["sessions"][0]
        assert (row["sdk_session"], row["model"]) == ("sdk-1", "haiku")


async def test_shutdown_closes_every_backend(tmp_path):
    async with Rig(tmp_path, fake_factory) as rig:
        a, sid = await make_session(rig)
        backend = rig.server.manager.sessions[sid].backend
        await rig.server.shutdown()
        assert backend.closed is True and rig.server.manager.sessions == {}


async def test_oversized_command_is_refused_not_buffered(tmp_path):
    async with Rig(tmp_path, fake_factory) as rig:
        a = await rig.client()
        await a.hello()
        a.w.write(b'{"type":"send","text":"' + b"x" * (UP_LINE_LIMIT + 10) + b'"}\n')
        assert "too large" in (await a.until("Error", timeout=5))["text"]


async def test_organs_in_the_snapshot_come_and_go_with_real_sdk_output(tmp_path):
    """Fed from SdkMapper, which emits OrganStarted with task_id None and learns the task id later."""
    sdk = pytest.importorskip("claude_agent_sdk")
    from orchestra.sdk_map import SdkMapper
    m = SdkMapper()
    msgs = [sdk.AssistantMessage([sdk.ToolUseBlock("A1", "Agent", {"subagent_type": "gobble", "description": "map it"})], "haiku"),
            sdk.TaskStartedMessage("task_started", {}, "T1", "map it", "u", "s1", tool_use_id="A1"),
            sdk.TaskProgressMessage("task_progress", {}, "T1", "map it", {"total_tokens": 9}, "u", "s1", last_tool_name="Grep"),
            sdk.TaskNotificationMessage("task_notification", {}, "T1", "completed", "/o", "", "u", "s1")]
    out = [e for msg in msgs for e in m.map(msg) if not isinstance(e, (ev.ModelUsed, ev.Narration))]
    kinds = [type(e).__name__ for e in out]
    assert kinds == ["ToolCall", "OrganStarted", "OrganProgress", "OrganDone"], kinds

    class Scripted(FakeBackend):
        done = 0

        async def send(self, text):                     # releases events up to index `text`
            for e in out[self.done:int(text)]:
                self.emit(e)
            self.done = int(text)

    async def factory(_p):
        return Scripted()

    async with Rig(tmp_path, factory) as rig:
        a, sid = await make_session(rig)
        s = rig.server.manager.sessions[sid]
        a.send(type="send", session=sid, text="2")                    # ToolCall, OrganStarted
        await a.until("OrganStarted")
        assert [o["organ"] for o in s.snapshot()["organs"]] == ["gobble"]
        a.send(type="send", session=sid, text="3")                    # + OrganProgress: learns T1
        await a.until("OrganProgress")
        assert s.snapshot()["organs"][0]["task_id"] == "T1"
        a.send(type="send", session=sid, text="4")                    # + OrganDone
        await a.until("OrganDone")
        await asyncio.sleep(0.02)
        assert s.snapshot()["organs"] == []


async def test_a_backend_that_ends_on_its_own_is_closed(tmp_path):
    async with Rig(tmp_path, fake_factory) as rig:
        a, sid = await make_session(rig)
        backend = rig.server.manager.sessions[sid].backend
        backend._finish()                                             # the CLI went away
        await a.until("session_closed")
        assert backend.closed is True


async def test_failed_send_clears_busy(tmp_path):
    class Hurt(FakeBackend):
        async def send(self, text):
            raise OSError("pipe")

    async def factory(_p):
        return Hurt()

    async with Rig(tmp_path, factory) as rig:
        a, sid = await make_session(rig)
        a.send(type="send", session=sid, text="x")
        assert (await a.until("Error"))["text"] == "internal error"
        assert rig.server.manager.sessions[sid].snapshot()["busy"] is False


async def test_listener_directory_rules(tmp_path):
    server = Server(SessionManager(fake_factory), "t")
    loose = tmp_path / "loose"
    loose.mkdir(mode=0o755)
    loose.chmod(0o755)
    with pytest.raises(RuntimeError, match="not private"):                 # an existing dir of ours is never chmod'd for you
        await UnixListener(loose / "engine.sock").start(server.handle)
    assert stat.S_IMODE(loose.stat().st_mode) == 0o755
    fresh = UnixListener(tmp_path / "new" / "engine.sock")                 # one we create is made 0700
    await fresh.start(server.handle)
    assert stat.S_IMODE((tmp_path / "new").stat().st_mode) == 0o700
    await fresh.close()


async def test_a_live_unlocked_socket_is_not_unlinked(tmp_path):
    """An engine from before the lock existed still answers: the probe refuses to steal its path."""
    path = tmp_path / "engine.sock"
    old = await asyncio.start_unix_server(lambda r, w: w.close(), str(path))
    try:
        with pytest.raises(RuntimeError, match="already listening"):
            await UnixListener(path).start(Server(SessionManager(fake_factory), "t").handle)
        assert path.exists()
    finally:
        old.close()
        await old.wait_closed()
