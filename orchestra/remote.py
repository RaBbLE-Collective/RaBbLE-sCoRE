"""RemoteBackend: the Backend protocol over the engine socket (orchestra/serve.py), so the Textual UI
can run on a long-lived engine. The wire contract is in serve.py's module docstring.

It creates a session (or attaches to `session`), yields the engine's events as `Event`s, maps the
face's calls to commands, and after a dropped connection reconnects with `attach{since_seq}`.
Resume point = max(seq) over events received (never `attached.seq`, which arrives before the replay);
after a gap replay, asks already seen are dropped by id."""

import asyncio
import contextlib
import json
from pathlib import Path
from typing import Any

from . import events as ev
from .backend import _Queued
from .serve import LINE_LIMIT, PROTOCOL

_CONTROL = {"hello", "sessions", "session_created", "attached", "detached", "session_closed"}


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
        self._r: asyncio.StreamReader | None = None
        self._w: asyncio.StreamWriter | None = None
        self._pump: asyncio.Task | None = None
        self._closing = False

    async def _connect(self) -> None:
        self._r, self._w = await asyncio.open_unix_connection(self.socket, limit=LINE_LIMIT)
        self._cmd("hello", version=PROTOCOL, client=self.client)
        hello = await self._frame("hello")
        self.engine, self.features = hello.get("engine", ""), hello.get("features", [])
        if self.session is None:
            self._cmd("create_session", **self.create)
            self.session = (await self._frame("session_created"))["session"]
        else:
            self._cmd("attach", since_seq=self.last_seq or None)
        self._attached(await self._frame("attached"))     # calls made after start() are addressed

    def _attached(self, m: dict[str, Any]) -> None:
        self.role, self.snapshot = m.get("role", ""), m.get("snapshot", {})
        if m.get("gap"):
            self.last_seq = 0                # the ring no longer reaches us: the tail restarts our count

    async def _frame(self, kind: str) -> dict[str, Any]:
        while True:
            line = await self._r.readline()
            if not line:
                raise ConnectionError("engine closed the connection")
            m = json.loads(line)
            if m.get("type") == kind:
                return m
            if m.get("type") == "Error":
                raise ConnectionError(m.get("text", "engine error"))

    async def start(self) -> None:
        await self._connect()
        self._pump = asyncio.create_task(self._run())

    def _cmd(self, type_: str, **fields: Any) -> None:
        msg = {"type": type_, **({"session": self.session} if self.session else {}), **fields}
        if self._w is None or self._w.is_closing():
            raise ConnectionError("not connected to the engine")
        self._w.write(json.dumps({k: v for k, v in msg.items() if v is not None}, ensure_ascii=False).encode() + b"\n")

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
                if self._closing or not self.reconnect:
                    break
                while not self._closing:             # reattach with since_seq
                    tries += 1
                    if tries > self.max_retries:
                        self.emit(ev.Error("engine: connection lost"))
                        return
                    await asyncio.sleep(self.retry_delay)
                    try:
                        await self._connect()
                        break
                    except (ConnectionError, OSError, ValueError):
                        continue
            if not self._closing:
                self.emit(ev.Error("engine: connection lost"))
        finally:
            self._finish()

    async def _read(self) -> bool:
        """Read until the connection drops (False) or the session closes (True)."""
        while True:
            line = await self._r.readline()
            if not line:
                return False
            m = json.loads(line)
            kind = m.get("type")
            if kind == "session_closed":
                return True
            elif kind not in _CONTROL and (m.get("session") in (None, self.session)):
                self._deliver(m)

    def _deliver(self, m: dict[str, Any]) -> None:
        seq = m.get("seq")
        if seq is not None:
            if seq <= self.last_seq:
                return                               # already seen (overlapping replay)
            self.last_seq = max(self.last_seq, seq)
        try:
            e = ev.from_dict(m)             # envelope keys (session, seq) are not event fields; from_dict drops them
        except (ValueError, TypeError):
            return                                   # an event kind this client predates
        if isinstance(e, ev.PermissionAsk):
            if e.id in self._seen_asks:
                return                               # replayed after a gap
            self._seen_asks.add(e.id)
        self.emit(e)

    async def send(self, text: str) -> None:
        self._cmd("send", text=text)

    async def interrupt(self) -> None:
        self._cmd("interrupt")

    def answer(self, ask_id: str, reply: ev.PermissionReply) -> bool:
        try:
            self._cmd("answer", ask_id=ask_id, decision=reply.decision, reason=reply.reason)
        except ConnectionError:
            return False
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
                self._cmd("detach")
            self._w.close()
        if self._pump:
            self._pump.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._pump
        self._finish()
