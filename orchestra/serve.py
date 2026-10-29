"""The engine as a server: many sessions, many clients, one wire protocol.

  python -m orchestra.serve [--socket PATH] [--replay FILE]

NDJSON over a stream, one JSON object per line. Events go down exactly as
`Event.to_json()` writes them plus a `session` key; commands go up as
`{"type": "<command>", "session": "...", ...}`. Everything is addressed by session.
Plan + decisions: RaBbLE-Grimoire/log/handoffs/HANDOFF-S295-RaBbLE-UI-Rust-R1-Seam.md.

  down:  hello, sessions, session_created, attached, detached, session_closed,
         and every orchestra event (Error doubles as the error reply)
  up:    hello{version,client}, list_sessions, create_session{cwd,resume,continue,model,mode,
         crew,trust,provider}, attach{session}, detach{session}, close_session{session},
         grant{session,principal,role}, send{session,text}, interrupt, answer{ask_id,decision,reason},
         set_model{model}, set_permission_mode{mode}, set_trust{profile}, stop_task{task_id}

Identity comes from the transport (a Principal made by the Listener), never from a message.
Sessions outlive their clients; the first answer to a PermissionAsk wins.
Not built (undecided, see the handoff): accounts, tokens, pairing, hosted listener.
"""

import argparse
import asyncio
import json
import os
import signal
import socket
import struct
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from . import events as ev

PROTOCOL = 1
LINE_LIMIT = 64 * 1024 * 1024        # one tool result can be a base64 screenshot
ROLES = ("owner", "participant", "observer")
CAN_DRIVE = ("owner", "participant")  # may send / interrupt / answer / change settings


@dataclass(frozen=True)
class Principal:
    """Who is acting. Made by the transport, so a message can never claim to be someone else."""
    id: str
    kind: str = "local"


class Connection:
    """One client. Writes go through a queue so a slow client cannot stall the fan-out."""

    def __init__(self, writer: asyncio.StreamWriter, principal: Principal):
        self.writer = writer
        self.principal = principal
        self.client = ""
        self.ready = False                # hello done
        self.sessions: set[str] = set()
        self._out: asyncio.Queue = asyncio.Queue()
        self._task = asyncio.create_task(self._drain())

    def send(self, obj: dict[str, Any]) -> None:
        self._out.put_nowait(json.dumps(obj, ensure_ascii=False, separators=(",", ":")))

    async def _drain(self) -> None:
        try:
            while (line := await self._out.get()) is not None:
                self.writer.write(line.encode() + b"\n")
                await self.writer.drain()
        except (ConnectionError, OSError):
            pass

    async def close(self) -> None:
        self._out.put_nowait(None)
        try:
            await asyncio.wait_for(self._task, 1)
        except (TimeoutError, asyncio.CancelledError):
            self._task.cancel()
        self.writer.close()


@dataclass
class Session:
    id: str
    backend: Any
    owner: Principal
    params: dict[str, Any]
    acl: dict[str, str] = field(default_factory=dict)             # principal id -> role
    clients: set[Connection] = field(default_factory=set)
    # what a late-attaching client needs to catch up without replaying the transcript
    caps: ev.Event | None = None
    started: ev.Event | None = None
    entropy: ev.Event | None = None
    usage: ev.Event | None = None
    asks: dict[str, ev.Event] = field(default_factory=dict)
    pump: asyncio.Task | None = None

    def role(self, p: Principal) -> str | None:
        return "owner" if p == self.owner else self.acl.get(p.id)

    def observe(self, e: ev.Event) -> None:
        match e:
            case ev.Capabilities():
                self.caps = e
            case ev.SessionStarted() | ev.SessionResumed():
                self.started = e
            case ev.EntropyChanged():
                self.entropy = e
            case ev.Usage():
                self.usage = e
            case ev.PermissionAsk():
                self.asks[e.id] = e
            case ev.PermissionResolved():
                self.asks.pop(e.id, None)
            case ev.TurnDone():
                self.asks.clear()          # nothing can still be waiting once the turn ended

    def broadcast(self, e: ev.Event) -> None:
        msg = {"session": self.id, **e.to_dict()}
        for c in self.clients:
            c.send(msg)

    def replay_to(self, c: Connection) -> None:
        for e in (self.caps, self.started, self.entropy, self.usage, *self.asks.values()):
            if e is not None:
                c.send({"session": self.id, **e.to_dict()})


BackendFactory = Callable[[dict[str, Any]], Awaitable[Any]]


class SessionManager:
    def __init__(self, factory: BackendFactory):
        self.factory = factory
        self.sessions: dict[str, Session] = {}
        self._n = 0

    async def create(self, owner: Principal, params: dict[str, Any]) -> Session:
        self._n += 1
        sid = f"s{os.getpid():x}-{self._n}"
        backend = await self.factory(params)
        s = Session(sid, backend, owner, params)
        self.sessions[sid] = s
        s.pump = asyncio.create_task(self._pump(s))
        await backend.start()
        return s

    async def _pump(self, s: Session) -> None:
        try:
            async for e in s.backend.events():
                s.observe(e)
                s.broadcast(e)
        finally:
            for c in list(s.clients):
                c.send({"session": s.id, "type": "session_closed"})
                c.sessions.discard(s.id)
            s.clients.clear()
            self.sessions.pop(s.id, None)

    async def close(self, s: Session) -> None:
        await s.backend.close()
        if s.pump:
            try:
                await asyncio.wait_for(s.pump, 2)
            except (TimeoutError, asyncio.CancelledError):
                s.pump.cancel()

    def listing(self, p: Principal) -> list[dict[str, Any]]:
        return [{"session": s.id, "role": r, "clients": len(s.clients), "cwd": s.params.get("cwd", ""),
                 "model": s.params.get("model"), "provider": s.params.get("provider")}
                for s in self.sessions.values() if (r := s.role(p))]


class Server:
    def __init__(self, manager: SessionManager, engine: str = ""):
        self.manager = manager
        self.engine = engine

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, principal: Principal) -> None:
        conn = Connection(writer, principal)
        try:
            while line := await reader.readline():
                try:
                    msg = json.loads(line)
                    if not isinstance(msg, dict):
                        raise ValueError("a command is a JSON object")
                    await self.dispatch(conn, msg)
                except (ValueError, KeyError, TypeError) as e:
                    self.error(conn, f"bad command: {e}", msg.get("session", "") if isinstance(locals().get("msg"), dict) else "")
        except (ConnectionError, asyncio.LimitOverrunError, ValueError):
            pass
        finally:
            for sid in list(conn.sessions):                     # sessions outlive clients
                if s := self.manager.sessions.get(sid):
                    s.clients.discard(conn)
            await conn.close()

    @staticmethod
    def error(conn: Connection, message: str, session: str = "") -> None:
        conn.send({"session": session, **ev.Error(message).to_dict()})

    def _session(self, conn: Connection, msg: dict, need: tuple[str, ...] | None) -> Session | None:
        s = self.manager.sessions.get(str(msg.get("session", "")))
        if s is None:
            self.error(conn, f"no such session: {msg.get('session', '')!r}")
            return None
        role = s.role(conn.principal)
        if role is None or (need is not None and role not in need):
            self.error(conn, f"not allowed on session {s.id}", s.id)   # same answer for "exists" and "denied"
            return None
        return s

    async def dispatch(self, conn: Connection, msg: dict[str, Any]) -> None:
        kind = msg["type"]
        if kind == "hello":
            if msg.get("version") != PROTOCOL:
                self.error(conn, f"protocol {msg.get('version')!r} not supported (engine speaks {PROTOCOL})")
                return await conn.close()
            conn.client, conn.ready = str(msg.get("client", "")), True
            conn.send({"type": "hello", "version": PROTOCOL, "engine": self.engine, "user": conn.principal.id})
            return
        if not conn.ready:
            return self.error(conn, "say hello first")
        m = self.manager
        if kind == "list_sessions":
            conn.send({"type": "sessions", "sessions": m.listing(conn.principal)})
        elif kind == "create_session":
            s = await m.create(conn.principal, {k: v for k, v in msg.items() if k != "type"})
            conn.send({"type": "session_created", "session": s.id})
            self._attach(conn, s)
        elif kind == "attach":
            if s := self._session(conn, msg, ROLES):
                self._attach(conn, s)
        elif kind == "detach":
            if s := self._session(conn, msg, ROLES):
                s.clients.discard(conn)
                conn.sessions.discard(s.id)
                conn.send({"type": "detached", "session": s.id})
        elif kind == "close_session":
            if s := self._session(conn, msg, ("owner",)):
                await m.close(s)
        elif kind == "grant":
            if (s := self._session(conn, msg, ("owner",))) and msg["role"] in ROLES[1:]:
                s.acl[str(msg["principal"])] = msg["role"]
            elif s:
                self.error(conn, "role must be participant or observer", s.id)
        elif kind in ("send", "interrupt", "answer", "set_model", "set_permission_mode", "set_trust", "stop_task"):
            if s := self._session(conn, msg, CAN_DRIVE):
                await self._drive(conn, s, kind, msg)
        else:
            self.error(conn, f"unknown command: {kind!r}")

    def _attach(self, conn: Connection, s: Session) -> None:
        s.clients.add(conn)
        conn.sessions.add(s.id)
        conn.send({"type": "attached", "session": s.id, "role": s.role(conn.principal)})
        s.replay_to(conn)

    async def _drive(self, conn: Connection, s: Session, kind: str, msg: dict[str, Any]) -> None:
        b = s.backend
        if kind == "send":
            s.broadcast(ev.UserSaid(conn.principal.id, str(msg["text"])))
            await b.send(str(msg["text"]))
        elif kind == "interrupt":
            s.asks.clear()
            await b.interrupt()
        elif kind == "answer":
            reply = ev.PermissionReply(msg["decision"], str(msg.get("reason", "")))
            if b.answer(str(msg["ask_id"]), reply):
                e = ev.PermissionResolved(str(msg["ask_id"]), reply.decision, conn.principal.id)
                s.observe(e)
                s.broadcast(e)
            else:
                self.error(conn, f"ask {msg['ask_id']!r} is already resolved or unknown", s.id)
        elif kind == "set_model":
            await b.set_model(msg.get("model"))
        elif kind == "set_permission_mode":
            await b.set_permission_mode(str(msg["mode"]))
        elif kind == "set_trust":
            b.set_trust(msg.get("profile"))
        elif kind == "stop_task":
            await b.stop_task(str(msg["task_id"]))


# ── transports ──────────────────────────────────────────────────────────────

class Listener(Protocol):
    """A transport. It authenticates and hands the server (reader, writer, Principal)."""
    async def start(self, on_connect: Callable[[asyncio.StreamReader, asyncio.StreamWriter, Principal], Awaitable[None]]) -> None: ...
    async def close(self) -> None: ...


def default_socket() -> Path:
    run = os.environ.get("XDG_RUNTIME_DIR")
    return (Path(run) / "rabble" if run else Path.home() / ".local" / "state" / "rabble") / "engine.sock"


class UnixListener:
    """Local transport. The principal is the connecting OS user (SO_PEERCRED); the socket is 0600."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._server: asyncio.AbstractServer | None = None

    async def start(self, on_connect) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.path.exists():
            try:                                                 # a live engine owns it; a dead one left it behind
                _, w = await asyncio.open_unix_connection(str(self.path))
            except (ConnectionRefusedError, FileNotFoundError):
                self.path.unlink(missing_ok=True)
            else:
                w.close()
                await w.wait_closed()
                raise RuntimeError(f"an engine is already listening on {self.path}")

        async def accept(reader, writer):
            sock = writer.get_extra_info("socket")
            try:
                _pid, uid, _gid = struct.unpack("3i", sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")))
            except OSError:
                writer.close()                                   # no verifiable identity, no service
                return
            await on_connect(reader, writer, Principal(f"local:{uid}", "local"))

        old = os.umask(0o177)
        try:
            self._server = await asyncio.start_unix_server(accept, str(self.path), limit=LINE_LIMIT)
        finally:
            os.umask(old)
        self.path.chmod(0o600)

    async def close(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()
        self.path.unlink(missing_ok=True)


# ── entry ───────────────────────────────────────────────────────────────────

def replay_factory(path: str, delay: float = 0.0) -> BackendFactory:
    from .backend import ReplayBackend

    async def make(_params: dict[str, Any]):
        return ReplayBackend.from_file(path, delay)
    return make


def claude_factory() -> BackendFactory:
    """Real sessions: ClaudeCodeBackend with the Crew, trust and engine config, as `rabble` builds them."""
    from .backend import ClaudeCodeBackend
    from .config import EngineConfig
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "crew"))

    async def make(p: dict[str, Any]):
        cwd = os.path.abspath(os.path.expanduser(p.get("cwd") or "."))
        lead = agents = None
        try:
            from roster import CREW_SUBDIR, build_agents, build_ui_lead_prompt, grimoire_root, load_crew
            grimoire = grimoire_root()
            personas = load_crew(grimoire / CREW_SUBDIR)
            crew = p.get("crew") is True
            lead = build_ui_lead_prompt(personas, grimoire, crew)
            agents = build_agents(personas) if crew else None
        except Exception as e:
            sys.stderr.write(f"orchestra.serve: no lead persona ({e}); starting plain\n")
        return ClaudeCodeBackend(cwd, append=lead, agents=agents, model=p.get("model"), permission_mode=p.get("mode"),
                                 resume=p.get("resume"), continue_conversation=p.get("continue") is True,
                                 trust_override=p.get("trust"), engine=EngineConfig.load(cwd))
    return make


async def serve(listener: Listener, factory: BackendFactory, engine: str = "") -> Server:
    server = Server(SessionManager(factory), engine)
    await listener.start(server.handle)
    return server


async def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m orchestra.serve", description=__doc__.split("\n")[0])
    p.add_argument("--socket", type=Path, default=default_socket())
    p.add_argument("--replay", metavar="FILE", help="serve a recorded turn file for every session (no model calls)")
    p.add_argument("--replay-delay", type=float, default=0.0)
    a = p.parse_args(argv)
    factory = replay_factory(a.replay, a.replay_delay) if a.replay else claude_factory()
    listener = UnixListener(a.socket)
    await serve(listener, factory, engine="replay" if a.replay else "claude-code")
    sys.stderr.write(f"orchestra.serve: listening on {a.socket}\n")
    stop = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        asyncio.get_running_loop().add_signal_handler(sig, stop.set)
    try:
        await stop.wait()
    finally:
        await listener.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
