"""RemoteBackend: the Backend protocol over the engine socket (orchestra/serve.py), so the Textual UI
can run on a long-lived engine. The wire contract is in serve.py's module docstring.

It creates a session (or attaches to `session`), yields the engine's events as `Event`s, maps the
face's calls to commands, and after a dropped connection reconnects with `attach{since_seq}`.
Resume point = max(seq) over events received (never `attached.seq`, which arrives before the replay);
after a gap replay, asks already seen are dropped by id."""

import asyncio
import contextlib
import json
from collections import deque
from pathlib import Path
from typing import Any

from . import events as ev
from .backend import _Queued
from .serve import LINE_LIMIT, PROTOCOL

_CONTROL = {"hello", "sessions", "session_created", "attached", "detached", "session_closed"}


class _Fatal(Exception):
    """The engine will not take us back (the session is gone): do not retry."""


class RemoteBackend(_Queued):
    def __init__(self, socket: str | Path, *, session: str | None = None, client: str = "rabble-ui",
                 create: dict[str, Any] | None = None, reconnect: bool = True,
                 retry_delay: float = 0.2, max_retries: int = 50):
        super().__init__()
        self.socket = str(socket)
        self.session = session            # None until create_session answers
        self.client = client
        self.create = create or {}        # cwd, resume, continue, model, mode, crew, trust, provider
        self.reconnect = reconnect
        self.retry_delay, self.max_retries = retry_delay, max_retries
        self.last_seq = 0
        self.role = ""
        self.snapshot: dict[str, Any] = {}
        self.engine = ""
        self.features: list[str] = []
        self._seen_asks: set[str] = set()
        self._open: set[str] = set()              # asks shown and not yet resolved
        self._gap_pending: set[str] = set()
        self._started = False
        self._live = False
        self._dead = False
        self._outbox: deque = deque(maxlen=256)   # commands made while disconnected
        self._r: asyncio.StreamReader | None = None
        self._w: asyncio.StreamWriter | None = None
        self._pump: asyncio.Task | None = None
        self._closing = False

    async def _connect(self) -> None:
        self._r, self._w = await asyncio.open_unix_connection(self.socket, limit=LINE_LIMIT)
        try:
            self._raw("hello", version=PROTOCOL, client=self.client)
            hello = await self._frame("hello")
            self.engine, self.features = hello.get("engine", ""), hello.get("features", [])
            if self.session is None:
                self._raw("create_session", **self.create)
                self.session = (await self._frame("session_created"))["session"]
            else:
                self._raw("attach", since_seq=self.last_seq or None)
            self._attached(await self._frame("attached"))     # calls made after start() are addressed
            while self._outbox:                                # what was said while away, in order
                t, f = self._outbox.popleft()
                self._raw(t, **f)
            self._live = True
        except BaseException:
            self._w.close()
            raise

    def _attached(self, m: dict[str, Any]) -> None:
        self.role, self.snapshot = m.get("role", ""), m.get("snapshot", {})
        if m.get("gap"):
            self.last_seq = 0                # the ring no longer reaches us: the tail restarts our count
            self._gap_pending = set(self._open)

    async def _frame(self, kind: str) -> dict[str, Any]:
        while True:
            line = await self._r.readline()
            if not line:
                raise ConnectionError("engine closed the connection")
            m = json.loads(line)
            if m.get("type") == kind:
                return m
            if m.get("type") == "Error":
                text = m.get("text", "engine error")
                raise _Fatal(text) if text.startswith("no such session") else ConnectionError(text)

    async def start(self) -> None:
        await self._connect()
        self._pump = asyncio.create_task(self._run())

    def _raw(self, type_: str, **fields: Any) -> None:
        msg = {"type": type_, **({"session": self.session} if self.session else {}), **fields}
        self._w.write(json.dumps({k: v for k, v in msg.items() if v is not None}, ensure_ascii=False).encode() + b"\n")

    def _cmd(self, type_: str, **fields: Any) -> None:
        """Never raises: while disconnected the command waits (bounded) and goes out after the reattach."""
        if self._live and self._w is not None and not self._w.is_closing():
            try:
                self._raw(type_, **fields)
                return
            except (OSError, RuntimeError):
                pass
        if self._dead:
            return                                   # the stream has ended: nothing to wait for
        if len(self._outbox) == self._outbox.maxlen:
            self.emit(ev.Error("engine: offline, oldest queued command dropped"))
        self._outbox.append((type_, fields))

    async def _run(self) -> None:
        tries = 0
        try:
            while not self._closing:
                try:
                    if await self._read():
                        return                       # the session ended
                    tries = 0
                except (ConnectionError, OSError, asyncio.IncompleteReadError, ValueError):
                    pass
                self._live = False
                if self._closing or not self.reconnect:
                    break
                while not self._closing:             # reattach with since_seq, backing off
                    tries += 1
                    if tries > self.max_retries:
                        self._outbox.clear()
                        self.emit(ev.Error("engine: connection lost"))
                        return
                    await asyncio.sleep(min(self.retry_delay * 2 ** min(tries - 1, 6), 5.0))
                    try:
                        await self._connect()
                        break
                    except _Fatal as e:
                        self._outbox.clear()
                        self.emit(ev.Error(f"engine: {e}"))
                        return
                    except (ConnectionError, OSError, ValueError):
                        continue
            if not self._closing:
                self._outbox.clear()
                self.emit(ev.Error("engine: connection lost"))
        finally:
            self._dead = True
            self._outbox.clear()
            self._finish()

    async def _read(self) -> bool:
        """Read until the connection drops (False) or the session ends (True)."""
        while True:
            if self._gap_pending:                    # end of the compact replay: a quiet moment
                try:
                    line = await asyncio.wait_for(self._r.readline(), 0.1)
                except asyncio.TimeoutError:
                    self._end_gap()
                    continue
            else:
                line = await self._r.readline()
            if not line:
                return False
            m = json.loads(line)
            kind = m.get("type")
            if kind == "session_closed":
                return True
            if kind == "detached":                   # we never asked: revoked
                if not self._closing:
                    self.emit(ev.Error("engine: detached"))
                return True
            if kind not in _CONTROL and (m.get("session") in (None, self.session)):
                self._deliver(m)

    def _end_gap(self) -> None:
        """Asks we showed that the compact state no longer holds were resolved while we were away."""
        for aid in sorted(self._gap_pending):
            self._open.discard(aid)
            self.emit(ev.PermissionResolved(aid, "deny", "engine"))
        self._gap_pending = set()

    def _deliver(self, m: dict[str, Any]) -> None:
        seq = m.get("seq")
        if seq is not None:
            if seq <= self.last_seq:
                return                               # already seen (overlapping replay)
            self.last_seq = max(self.last_seq, seq)
            if self._gap_pending:
                self._end_gap()                      # the tail has begun; the compact state is over
        try:
            e = ev.from_dict(m)             # envelope keys (session, seq) are not event fields; from_dict drops them
        except (ValueError, TypeError):
            return                                   # an event kind this client predates
        if isinstance(e, ev.PermissionAsk):
            self._gap_pending.discard(e.id)
            if e.id in self._seen_asks:
                return                               # replayed after a gap
            self._seen_asks.add(e.id)
            self._open.add(e.id)
        elif isinstance(e, ev.PermissionResolved):
            self._open.discard(e.id)
            self._gap_pending.discard(e.id)
        elif isinstance(e, ev.SessionStarted):
            if seq is None and self._started:
                return                               # the compact state's banner, shown once already
            self._started = True
        self.emit(e)

    async def send(self, text: str) -> None:
        self._cmd("send", text=text)

    async def interrupt(self) -> None:
        self._cmd("interrupt")

    def answer(self, ask_id: str, reply: ev.PermissionReply) -> bool:
        self._cmd("answer", ask_id=ask_id, decision=reply.decision, reason=reply.reason,
                  **({"answers": reply.answers} if reply.answers else {}))
        return True                                  # the first answer wins engine-side; PermissionResolved follows

    async def set_model(self, model: str | None) -> None:
        self._cmd("set_model", model=model)

    async def set_permission_mode(self, mode: str) -> None:
        self._cmd("set_permission_mode", mode=mode)

    def set_trust(self, profile: str | None) -> None:
        self._cmd("set_trust", profile=profile)

    async def stop_task(self, task_id: str) -> None:
        self._cmd("stop_task", task_id=task_id)

    async def close(self) -> None:
        """Detach; the session outlives this client."""
        self._closing = True
        if self._w is not None:
            with contextlib.suppress(Exception):
                self._raw("detach")
            self._w.close()
        if self._pump:
            self._pump.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._pump
        self._finish()
