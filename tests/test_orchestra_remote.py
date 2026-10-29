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


async def test_reconnects_with_since_seq(tmp_path):
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
        await rb.close()


async def test_gap_replay_dedupes_asks(tmp_path):
    import orchestra.serve as serve_mod
    async with Rig(tmp_path, fake_factory) as rig:
        driver = await rig.client()
        await driver.hello()
        driver.send(type="create_session")
        sid = (await driver.until("session_created"))["session"]
        rb = remote(rig, session=sid, retry_delay=0.05)
        await rb.start()
        driver.send(type="send", session=sid, text="x")
        got = await take(rb, 3)                       # SessionStarted, UserSaid, PermissionAsk
        assert isinstance(got[2], ev.PermissionAsk)
        s = rig.server.manager.sessions[sid]
        s.ring.clear()                                # the ring forgets what the client saw
        rb.last_seq = 1
        rb._w.close()
        more = await take(rb, 1)                      # the gap replay: compact state (SessionStarted), ask already seen
        assert isinstance(more[0], ev.SessionStarted)
        await asyncio.sleep(0.2)
        assert rb._queue.empty()                      # the open ask was not delivered twice
        await rb.close()


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
