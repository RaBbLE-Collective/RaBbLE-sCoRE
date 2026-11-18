"""RemoteBackend (orchestra/remote.py) against a real serve.py on a Unix socket. No model calls.
  ~/.local/share/RaBbLE/rabble-ui/bin/python -m pytest tests/test_orchestra_remote.py"""

import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from orchestra import events as ev  # noqa: E402
from orchestra.remote import RemoteBackend  # noqa: E402
from test_orchestra_serve import FIXTURE, FakeBackend, Rig, fake_factory  # noqa: E402
import orchestra.serve as serve_mod  # noqa: E402
from orchestra.serve import replay_factory  # noqa: E402

pytestmark = pytest.mark.asyncio


class _Track:
    """Lets the Rig close the backend's current writer on exit (wait_closed waits on open connections)."""
    def __init__(self, rb):
        self.rb = rb

    @property
    def w(self):
        return self.rb._w


def remote(rig, *a, **kw):
    rb = RemoteBackend(rig.path, *a, **kw)
    rig.clients.append(_Track(rb))
    return rb


async def take(rb, n, timeout=3.0):
    it = rb.events()
    return [await asyncio.wait_for(it.__anext__(), timeout) for _ in range(n)]


async def test_creates_session_and_maps_commands(tmp_path):
    backends = []

    async def factory(_p):
        backends.append(FakeBackend())
        return backends[-1]
    async with Rig(tmp_path, factory) as rig:
        rb = remote(rig)
        await rb.start()
        (started,) = await take(rb, 1)
        assert isinstance(started, ev.SessionStarted) and rb.role == "owner"
        await rb.send("hi")
        said, ask = await take(rb, 2)
        assert isinstance(said, ev.UserSaid) and said.text == "hi" and said.client == "rabble-ui"
        assert isinstance(ask, ev.PermissionAsk) and ask.id == "a1"
        assert rb.answer("a1", ev.PermissionReply("allow"))
        (res,) = await take(rb, 1)
        assert isinstance(res, ev.PermissionResolved) and res.decision == "allow"
        await rb.set_model("opus")
        await rb.set_permission_mode("plan")
        rb.set_trust("strict")
        await asyncio.sleep(0.2)
        assert backends[0].calls == [("set_model", "opus"), ("set_permission_mode", "plan"), ("set_trust", "strict")]
        await rb.close()
        assert backends[0].closed is False        # detach leaves the session running


async def test_second_client_sees_what_the_first_drives(tmp_path):
    async with Rig(tmp_path, fake_factory) as rig:
        driver = await rig.client()
        await driver.hello("driver")
        driver.send(type="create_session")
        sid = (await driver.until("session_created"))["session"]
        rb = remote(rig, session=sid, client="rabble-ui")
        await rb.start()
        (started,) = await take(rb, 1)
        assert isinstance(started, ev.SessionStarted)
        driver.send(type="send", session=sid, text="from the other client")
        said, ask = await take(rb, 2)
        assert said.text == "from the other client" and said.client == "driver"
        assert ask.id == "a1"
        # the second client answers; the driver sees it resolved
        assert rb.answer("a1", ev.PermissionReply("deny"))
        resolved = await driver.until("PermissionResolved")
        assert resolved["decision"] == "deny"
        await rb.close()


async def test_reconnects_with_since_seq(tmp_path, monkeypatch):
    seen_since = []
    real = serve_mod.Session.replay_to

    def spy(self, c, since, rid):
        seen_since.append(since)
        return real(self, c, since, rid)
    monkeypatch.setattr(serve_mod.Session, "replay_to", spy)
    async with Rig(tmp_path, fake_factory) as rig:
        driver = await rig.client()
        await driver.hello()
        driver.send(type="create_session")
        sid = (await driver.until("session_created"))["session"]
        rb = remote(rig, session=sid, retry_delay=0.05)
        await rb.start()
        await take(rb, 1)                             # SessionStarted, seq 1
        assert rb.last_seq == 1
        rb._w.close()                                 # the connection drops
        driver.send(type="send", session=sid, text="while away")
        await asyncio.sleep(0.05)
        said, ask = await take(rb, 2)                 # no SessionStarted again: only what was missed
        assert said.text == "while away" and isinstance(ask, ev.PermissionAsk)
        assert rb.last_seq == 3
        assert seen_since[-1] == 1                    # attach carried since_seq, not None
        await rb.close()


async def test_gap_replay_dedupes_asks(tmp_path):
    async with Rig(tmp_path, fake_factory) as rig:
        driver = await rig.client()
        await driver.hello()
        driver.send(type="create_session")
        sid = (await driver.until("session_created"))["session"]
        rb = remote(rig, session=sid, retry_delay=0.05)
        raw = []
        real = rb._deliver
        rb._deliver = lambda m: (raw.append(m), real(m))[1]
        await rb.start()
        driver.send(type="send", session=sid, text="x")
        got = await take(rb, 3)                       # SessionStarted, UserSaid, PermissionAsk
        assert isinstance(got[2], ev.PermissionAsk)
        s = rig.server.manager.sessions[sid]
        assert "a1" in s.asks
        s.ring.clear()                                # the ring forgets what the client saw
        raw.clear()
        rb.last_seq = 1
        rb._w.close()
        await asyncio.sleep(0.4)
        assert any(m["type"] == "PermissionAsk" and "seq" not in m for m in raw)   # the compact state carried the ask
        assert rb._queue.empty()                      # neither the ask nor the banner shown twice
        await rb.close()


async def test_gap_resolves_asks_that_vanished(tmp_path):
    backends = []

    async def factory(_p):
        backends.append(FakeBackend())
        return backends[-1]
    async with Rig(tmp_path, factory) as rig:
        driver = await rig.client()
        await driver.hello()
        driver.send(type="create_session")
        sid = (await driver.until("session_created"))["session"]
        rb = remote(rig, session=sid, retry_delay=0.3)
        await rb.start()
        driver.send(type="send", session=sid, text="x")
        await take(rb, 3)
        rb._w.close()
        await asyncio.sleep(0.05)
        driver.send(type="answer", session=sid, ask_id="a1", decision="allow")
        await driver.until("PermissionResolved")
        rig.server.manager.sessions[sid].ring.clear()
        (res,) = await take(rb, 1)
        assert isinstance(res, ev.PermissionResolved) and res.id == "a1"
        await rb.close()


async def test_commands_while_disconnected_are_queued_and_flushed(tmp_path):
    backends = []

    async def factory(_p):
        backends.append(FakeBackend())
        return backends[-1]
    async with Rig(tmp_path, factory) as rig:
        rb = remote(rig, retry_delay=0.3)
        await rb.start()
        await take(rb, 1)
        await rb.send("go")
        await take(rb, 2)                             # UserSaid, ask a1
        rb._w.close()
        await asyncio.sleep(0.05)                     # the drop is noticed
        assert rb.answer("a1", ev.PermissionReply("allow")) is True    # never False, never raises
        rb.set_trust("strict")
        await rb.set_model("opus")
        await asyncio.sleep(0.8)                      # reattach and flush
        assert backends[0].answered and backends[0].answered[0][0] == "a1"
        assert backends[0].calls == [("set_trust", "strict"), ("set_model", "opus")]
        await rb.close()


async def test_exhausted_retries_drop_commands_without_raising(tmp_path):
    async with Rig(tmp_path, fake_factory) as rig:
        rb = remote(rig, retry_delay=0.01, max_retries=2)
        await rb.start()
        await take(rb, 1)
        rig.srv.close()                               # no new connections
        rb._w.close()
        await asyncio.sleep(0.05)
        rb.set_trust("x")
        assert rb.answer("a1", ev.PermissionReply("deny")) is True
        (err,) = await take(rb, 1, timeout=5)
        assert isinstance(err, ev.Error) and "connection lost" in err.text
        rb.set_trust("y")                             # after the end: still no raise
        assert not rb._outbox


async def test_vanished_session_is_final_not_retried(tmp_path):
    async with Rig(tmp_path, fake_factory) as rig:
        rb = remote(rig, retry_delay=0.01, max_retries=50)
        await rb.start()
        await take(rb, 1)
        rig.server.manager.sessions.clear()           # the engine forgot the session
        rb._w.close()
        (err,) = await take(rb, 1, timeout=2)
        assert isinstance(err, ev.Error) and "no such session" in err.text


async def test_revoke_emits_detached_error_and_ends(tmp_path):
    async with Rig(tmp_path, fake_factory) as rig:
        rig.principals = ["local:1", "local:2"]
        owner = await rig.client()
        await owner.hello()
        owner.send(type="create_session")
        sid = (await owner.until("session_created"))["session"]
        owner.send(type="grant", session=sid, principal="local:2", role="participant")
        await asyncio.sleep(0.05)
        rb = remote(rig, session=sid)
        await rb.start()
        await take(rb, 1)
        owner.send(type="revoke", session=sid, principal="local:2")
        (err,) = await take(rb, 1)
        assert isinstance(err, ev.Error) and err.text == "engine: detached"
        assert [e async for e in rb.events()] == []   # the stream ended


async def test_replay_engine_runs_a_turn(tmp_path):
    async with Rig(tmp_path, replay_factory(FIXTURE)) as rig:
        rb = remote(rig)
        await rb.start()
        await rb.send("go")
        seen = []
        async for e in rb.events():
            seen.append(e)
            if isinstance(e, ev.TurnDone):
                break
        assert any(isinstance(e, ev.ToolCall) for e in seen)
        await rb.close()
