"""The engine as a server: many sessions, many clients, one wire protocol.

  python -m orchestra.serve [--socket PATH] [--replay FILE]

NDJSON over a stream, one JSON object per line. Events go down exactly as
`Event.to_json()` writes them plus a `session` key and a per-session `seq`; commands go up as
`{"type": "<command>", "session": "...", "id": "<optional>", ...}`. Everything is addressed by
session. Plan + decisions: RaBbLE-Grimoire/log/handoffs/HANDOFF-S295-RaBbLE-UI-Rust-R1-Seam.md,
protocol v1.1 freeze: HANDOFF-S295-RaBbLE-UI-Rust-Phase2-Parallel.md.

  down:  hello{version,engine,user,features}, sessions, session_created, attached{role,seq,gap,snapshot},
         detached, session_closed, and every orchestra event (Error doubles as the error reply)
  up:    hello{version,client}, list_sessions, create_session{cwd,resume,continue,model,mode,
         crew,trust,provider}, attach{session,since_seq?}, detach{session}, close_session{session},
         grant{session,principal,role}, revoke{session,principal}, send{session,text},
         interrupt, answer{ask_id,decision,reason}, set_model{model}, set_permission_mode{mode},
         set_trust{profile}, stop_task{task_id}

Versioning: additive changes never bump `version` (hello.features says what this engine has); an
engine refuses a client only below MIN_PROTOCOL, and a client decides for itself whether the
engine's `version` is enough.

Sequencing: every event of a session gets `seq` (1, 2, ...) and lands in a bounded ring buffer
(by count and bytes). `attach` replays the ring (all of it, or what follows `since_seq`); if the
client's `since_seq` is older than the ring, `attached.gap` is true and a compact state
(Capabilities, SessionStarted, EntropyChanged, merged Usage, open asks) goes before the tail.
`attached.seq` is the last seq the replay reaches; `attached.snapshot` is the live state
(busy, turn_owner, model, mode, trust, organs, sdk_session_id).

Replies: an optional `id` on any command is echoed on its reply (or error). Events a command causes
carry no id. `create_session{resume: "<sdk_session_id>"}` resumes a Claude session; the SDK id of
every session is in `snapshot.sdk_session_id` and `sessions[].sdk_session`.

Safety: settings (model, permission mode, trust) are owner-only; `create_session` goes through
`policy(principal, command, params)` (return params, possibly rewritten, or None to deny; the
local policy allows local principals and nobody else). A project `.rabble/engine.json` is only
loaded for local principals. A client that stops reading is disconnected once its queue passes
MAX_QUEUE_BYTES, with a notice; it reattaches with `since_seq`.

Identity comes from the transport (a Principal made by the Listener), never from a message.
Sessions outlive their clients; the first answer to a PermissionAsk wins.
Not built (undecided, see the handoff): accounts, tokens, pairing, hosted listener, and
refusing answers from the session CLI's own process tree (finding 12, deferred).
"""

import argparse
import asyncio
import fcntl
import json
import os
import secrets
import signal
import socket
import stat
import struct
import sys
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from . import events as ev

PROTOCOL = 1
MIN_PROTOCOL = 1
FEATURES = ["seq", "attach_since", "request_id", "snapshot", "user_said_client", "owner_settings", "policy", "revoke"]
UP_LINE_LIMIT = 1024 * 1024            # a command is small
LINE_LIMIT = 64 * 1024 * 1024          # what a client must accept down: one tool result can be a base64 screenshot
MAX_QUEUE_BYTES = 32 * 1024 * 1024     # per client; past it the client is disconnected (finding 6)
RING_EVENTS = 4000                     # per session replay buffer
RING_BYTES = 8 * 1024 * 1024
ROLES = ("owner", "participant", "observer")
CAN_DRIVE = ("owner", "participant")  # may send / interrupt / answer / stop tasks
OWNER_ONLY = ("owner",)               # settings, close, grant, revoke
DECISIONS = ("allow", "allow_session", "deny")
CREATE_KEYS = ("cwd", "resume", "continue", "model", "mode", "crew", "trust", "provider")
_STR_KEYS = ("cwd", "resume", "model", "mode", "trust", "provider")
_BOOL_KEYS = ("continue", "crew")
UNKNOWN = "no such session"           # one answer for "absent" and "not yours"


@dataclass(frozen=True)
class Principal:
    """Who is acting. Made by the transport, so a message can never claim to be someone else."""
    id: str
    kind: str = "local"


Policy = Callable[[Principal, str, dict[str, Any]], dict[str, Any] | None]


def local_policy(principal: Principal, command: str, params: dict[str, Any]) -> dict[str, Any] | None:
    """The stub: local principals may do anything; every other kind is refused until a real
    policy (accounts, quotas, cwd confinement) is decided. Return params, or None to deny."""
    return params if principal.kind == "local" else None


def _encode(obj: dict[str, Any]) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


class Connection:
    """One client. Writes go through a byte-capped queue so a slow client cannot stall the fan-out
    or grow without bound: past MAX_QUEUE_BYTES it gets a notice and is disconnected."""

    def __init__(self, writer: asyncio.StreamWriter, principal: Principal, max_queue: int | None = None):
        self.writer = writer
        self.principal = principal
        self.client = ""
        self.ready = False                # hello done
        self.dead = False
        self.sessions: set[str] = set()
        self.max_queue = MAX_QUEUE_BYTES if max_queue is None else max_queue
        self.queued = 0
        self.tasks: set[asyncio.Task] = set()
        self._out: asyncio.Queue = asyncio.Queue()
        self._task = asyncio.create_task(self._drain())

    def send(self, obj: dict[str, Any]) -> None:
        self.send_line(_encode(obj))

    def send_line(self, line: str) -> None:
        if self.dead:
            return
        n = len(line) + 1
        if self.queued + n > self.max_queue:
            self._overflow()
            return
        self.queued += n
        self._out.put_nowait((line, n))

    def _overflow(self) -> None:
        self.dead = True
        self._task.cancel()
        notice = _encode(ev.Error("client too slow: disconnected; reattach with since_seq to catch up").to_dict())
        try:
            self.writer.write(notice.encode() + b"\n")
            self.writer.close()
            # a peer that never reads would keep close() waiting for the flush
            asyncio.get_running_loop().call_later(1.0, self.writer.transport.abort)
        except (ConnectionError, OSError, RuntimeError):
            pass

    async def _drain(self) -> None:
        try:
            while (item := await self._out.get()) is not None:
                line, n = item
                self.writer.write(line.encode() + b"\n")
                self.queued -= n
                await self.writer.drain()
        except (ConnectionError, OSError):
            pass

    async def close(self) -> None:
        self.dead = True
        for t in list(self.tasks):
            t.cancel()
        if not self._task.done():
            self._out.put_nowait(None)
            try:
                await asyncio.wait_for(self._task, 1)
            except (TimeoutError, asyncio.CancelledError):
                self._task.cancel()
        try:
            self.writer.close()
        except (ConnectionError, OSError):
            pass


@dataclass
class Session:
    id: str
    backend: Any
    owner: Principal
    params: dict[str, Any]
    acl: dict[str, str] = field(default_factory=dict)             # principal id -> role
    clients: set[Connection] = field(default_factory=set)
    # compact state, for a client whose since_seq fell off the ring
    caps: ev.Event | None = None
    started: ev.Event | None = None
    entropy: ev.Event | None = None
    usage: ev.Usage | None = None
    asks: dict[str, ev.Event] = field(default_factory=dict)
    pump: asyncio.Task | None = None
    # sequencing + replay
    seq: int = 0
    ring: deque = field(default_factory=deque)                    # (seq, line, bytes)
    ring_bytes: int = 0
    ring_events: int = RING_EVENTS
    ring_cap: int = RING_BYTES
    # live state for the snapshot
    busy: bool = False
    turn_owner: str | None = None
    model: str | None = None
    mode: str | None = None
    trust: str | None = None
    sdk_session_id: str | None = None
    organs: dict[str, dict[str, Any]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.model = self.params.get("model")
        self.mode = self.params.get("mode")
        self.trust = self.params.get("trust")

    def role(self, p: Principal) -> str | None:
        return "owner" if p == self.owner else self.acl.get(p.id)

    def pending_asks(self) -> set[str]:
        """What the backend says is really waiting for an answer. A backend with no permission
        machinery (replay) has nothing waiting."""
        pending = getattr(getattr(self.backend, "permissions", None), "pending", None)
        return set(pending) if pending is not None else set()

    def observe(self, e: ev.Event) -> None:
        match e:
            case ev.Capabilities():
                self.caps = e
            case ev.SessionStarted() | ev.SessionResumed():
                self.started = e
                self.sdk_session_id = e.session_id
                if isinstance(e, ev.SessionStarted):
                    self.model = e.model
            case ev.ModelUsed():
                self.model = e.model
            case ev.EntropyChanged():
                self.entropy = e
                self.trust = e.trust
            case ev.Usage():
                prev = self.usage                     # None means "unchanged", not "zero"
                self.usage = ev.Usage(e.tokens,
                                      e.cost if e.cost is not None else (prev.cost if prev else None),
                                      e.context_pct if e.context_pct is not None else (prev.context_pct if prev else None))
            case ev.PermissionAsk():
                self.asks[e.id] = e
            case ev.PermissionResolved():
                self.asks.pop(e.id, None)
            case ev.OrganStarted():
                self.organs[e.task_id or e.tool_use_id] = {"organ": e.organ, "task_id": e.task_id, "brief": e.brief}
            case ev.OrganDone():
                self.organs.pop(e.task_id, None)
            case ev.TurnDone():
                if e.origin == "observer":
                    self.busy, self.turn_owner = False, None

    def publish(self, e: ev.Event) -> None:
        """Observe, sequence, remember and fan out one event."""
        self.observe(e)
        self.seq += 1
        line = _encode({"session": self.id, "seq": self.seq, **e.to_dict()})
        n = len(line) + 1
        self.ring.append((self.seq, line, n))
        self.ring_bytes += n
        while self.ring and (len(self.ring) > self.ring_events or self.ring_bytes > self.ring_cap):
            self.ring_bytes -= self.ring.popleft()[2]
        for c in list(self.clients):
            if c.dead:
                self.clients.discard(c)
            else:
                c.send_line(line)
        if isinstance(e, ev.TurnDone):
            self.reconcile_asks()

    def reconcile_asks(self, by: str = "engine") -> None:
        """The backend's pending set is the truth; tell everyone about asks that are gone."""
        pending = self.pending_asks()
        for aid in [a for a in self.asks if a not in pending]:
            self.publish(ev.PermissionResolved(aid, "deny", by))

    def snapshot(self) -> dict[str, Any]:
        return {"busy": self.busy, "turn_owner": self.turn_owner, "model": self.model, "mode": self.mode,
                "trust": self.trust, "organs": list(self.organs.values()), "sdk_session_id": self.sdk_session_id}

    def replay_to(self, c: Connection, since: int | None, rid: str | None) -> None:
        """attached, then the events the client is missing (see the module docstring)."""
        since = 0 if since is None else since
        first = self.ring[0][0] if self.ring else self.seq + 1
        gap = since + 1 < first or since > self.seq
        msg: dict[str, Any] = {"type": "attached", "session": self.id, "role": self.role(c.principal), "seq": self.seq,
                               "gap": gap, "snapshot": self.snapshot()}
        if rid is not None:
            msg["id"] = rid
        c.send(msg)
        if gap:
            for e in (self.caps, self.started, self.entropy, self.usage, *self.asks.values()):
                if e is not None:
                    c.send({"session": self.id, **e.to_dict()})
        for seq, line, _n in self.ring:
            if seq > since or gap:
                c.send_line(line)


BackendFactory = Callable[[dict[str, Any]], Awaitable[Any]]


class SessionManager:
    def __init__(self, factory: BackendFactory):
        self.factory = factory
        self.sessions: dict[str, Session] = {}

    async def create(self, owner: Principal, params: dict[str, Any]) -> Session:
        """Start the backend first; only a session that started is registered (no zombies)."""
        backend = await self.factory({**params, "principal_kind": owner.kind})
        try:
            await backend.start()
        except BaseException:
            try:
                await backend.close()
            except Exception:
                pass
            raise
        sid = "s" + secrets.token_hex(6)
        s = Session(sid, backend, owner, params)
        self.sessions[sid] = s
        s.pump = asyncio.create_task(self._pump(s))
        return s

    async def _pump(self, s: Session) -> None:
        try:
            async for e in s.backend.events():
                s.publish(e)
        except Exception as e:                                  # a broken backend ends the session, not the engine
            sys.stderr.write(f"orchestra.serve: session {s.id} backend failed: {e!r}\n")
        finally:
            for c in list(s.clients):
                c.send({"session": s.id, "type": "session_closed"})
                c.sessions.discard(s.id)
            s.clients.clear()
            self.sessions.pop(s.id, None)

    async def close(self, s: Session) -> None:
        try:
            await s.backend.close()
        finally:
            if s.pump:
                try:
                    await asyncio.wait_for(s.pump, 2)
                except (TimeoutError, asyncio.CancelledError):
                    s.pump.cancel()

    async def close_all(self) -> None:
        """Engine shutdown: close every backend so CLI children do not outlive us."""
        for s in list(self.sessions.values()):
            try:
                await self.close(s)
            except Exception as e:
                sys.stderr.write(f"orchestra.serve: closing {s.id}: {e!r}\n")

    def listing(self, p: Principal) -> list[dict[str, Any]]:
        return [{"session": s.id, "role": r, "clients": len(s.clients), "cwd": s.params.get("cwd", ""),
                 "model": s.model, "provider": s.params.get("provider"), "sdk_session": s.sdk_session_id, "busy": s.busy}
                for s in self.sessions.values() if (r := s.role(p))]


class Server:
    def __init__(self, manager: SessionManager, engine: str = "", policy: Policy = local_policy):
        self.manager = manager
        self.engine = engine
        self.policy = policy
        self.conns: set[Connection] = set()

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, principal: Principal) -> None:
        conn = Connection(writer, principal)
        self.conns.add(conn)
        try:
            while True:
                try:
                    line = await reader.readline()
                except ValueError:                                 # past UP_LINE_LIMIT: the stream is no longer in step
                    self.error(conn, f"command too large (limit {UP_LINE_LIMIT} bytes)")
                    break
                if not line:
                    break
                msg: dict[str, Any] | None = None
                try:
                    parsed = json.loads(line)
                    if not isinstance(parsed, dict):
                        raise ValueError("a command is a JSON object")
                    msg = parsed
                    await self.dispatch(conn, msg)
                except (ValueError, KeyError, TypeError) as e:
                    self.error(conn, f"bad command: {e}", *self._where(msg))
                except Exception as e:                             # an engine fault must not kill the connection
                    sys.stderr.write(f"orchestra.serve: command failed: {e!r}\n")
                    self.error(conn, "internal error", *self._where(msg))
        except (ConnectionError, asyncio.CancelledError):
            pass
        finally:
            self.conns.discard(conn)
            for sid in list(conn.sessions):                     # sessions outlive clients
                if s := self.manager.sessions.get(sid):
                    s.clients.discard(conn)
            await conn.close()

    @staticmethod
    def _where(msg: dict[str, Any] | None) -> tuple[str, str | None]:
        if not msg:
            return "", None
        rid = msg.get("id")
        return str(msg.get("session", "")), rid if isinstance(rid, str) else None

    @staticmethod
    def error(conn: Connection, message: str, session: str = "", rid: str | None = None) -> None:
        out = {"session": session, **ev.Error(message).to_dict()}
        if rid is not None:
            out["id"] = rid
        conn.send(out)

    def _session(self, conn: Connection, msg: dict, need: tuple[str, ...] | None) -> Session | None:
        rid = msg.get("id") if isinstance(msg.get("id"), str) else None
        sid = str(msg.get("session", ""))
        s = self.manager.sessions.get(sid)
        role = s.role(conn.principal) if s else None
        if s is None or role is None:
            self.error(conn, f"{UNKNOWN}: {sid!r}", "", rid)      # identical for "absent" and "not yours"
            return None
        if need is not None and role not in need:
            self.error(conn, f"not allowed on session {s.id}", s.id, rid)
            return None
        return s

    async def dispatch(self, conn: Connection, msg: dict[str, Any]) -> None:
        kind = msg["type"]
        rid = msg.get("id") if isinstance(msg.get("id"), str) else None

        def reply(obj: dict[str, Any]) -> None:
            conn.send({**obj, "id": rid} if rid is not None else obj)

        if kind == "hello":
            v = msg.get("version")
            if not isinstance(v, int) or isinstance(v, bool) or v < MIN_PROTOCOL:
                self.error(conn, f"protocol {v!r} not supported (engine speaks {PROTOCOL}, minimum {MIN_PROTOCOL})", "", rid)
                return await conn.close()
            conn.client, conn.ready = str(msg.get("client", "")), True
            reply({"type": "hello", "version": PROTOCOL, "engine": self.engine, "user": conn.principal.id, "features": FEATURES})
            return
        if not conn.ready:
            return self.error(conn, "say hello first", "", rid)
        m = self.manager
        if kind == "list_sessions":
            reply({"type": "sessions", "sessions": m.listing(conn.principal)})
        elif kind == "create_session":
            t = asyncio.create_task(self._create(conn, msg, rid))      # one slow start must not hold the connection
            conn.tasks.add(t)
            t.add_done_callback(conn.tasks.discard)
        elif kind == "attach":
            if s := self._session(conn, msg, ROLES):
                since = msg.get("since_seq")
                if since is not None and (not isinstance(since, int) or isinstance(since, bool) or since < 0):
                    return self.error(conn, "since_seq must be a non-negative integer", s.id, rid)
                self._attach(conn, s, since, rid)
        elif kind == "detach":
            if s := self._session(conn, msg, ROLES):
                s.clients.discard(conn)
                conn.sessions.discard(s.id)
                reply({"type": "detached", "session": s.id})
        elif kind == "close_session":
            if s := self._session(conn, msg, OWNER_ONLY):
                await m.close(s)
        elif kind in ("grant", "revoke"):
            if s := self._session(conn, msg, OWNER_ONLY):
                who = str(msg["principal"])
                if kind == "revoke":
                    s.acl.pop(who, None)
                    for c in [c for c in s.clients if c.principal.id == who]:   # revoked means out, now
                        s.clients.discard(c)
                        c.sessions.discard(s.id)
                        c.send({"session": s.id, "type": "detached"})
                elif msg.get("role") in ROLES[1:]:
                    s.acl[who] = msg["role"]
                else:
                    self.error(conn, "role must be participant or observer", s.id, rid)
        elif kind in ("set_model", "set_permission_mode", "set_trust"):
            if s := self._session(conn, msg, OWNER_ONLY):               # a guest never changes the owner's settings
                await self._drive(conn, s, kind, msg, rid)
        elif kind in ("send", "interrupt", "answer", "stop_task"):
            if s := self._session(conn, msg, CAN_DRIVE):
                await self._drive(conn, s, kind, msg, rid)
        else:
            self.error(conn, f"unknown command: {kind!r}", "", rid)

    async def _create(self, conn: Connection, msg: dict[str, Any], rid: str | None) -> None:
        try:
            params: dict[str, Any] = {k: msg[k] for k in CREATE_KEYS if msg.get(k) is not None}
            for k in _STR_KEYS:
                if k in params and not isinstance(params[k], str):
                    raise ValueError(f"{k} must be a string")
            for k in _BOOL_KEYS:
                if k in params and not isinstance(params[k], bool):
                    raise ValueError(f"{k} must be true or false")
            allowed = self.policy(conn.principal, "create_session", params)
            if allowed is None:
                return self.error(conn, "not allowed: create_session", "", rid)
            s = await self.manager.create(conn.principal, {k: v for k, v in allowed.items() if k in CREATE_KEYS})
        except (ValueError, KeyError, TypeError) as e:
            return self.error(conn, f"bad command: {e}", "", rid)
        except Exception as e:                                          # failed start: nothing was registered
            sys.stderr.write(f"orchestra.serve: create_session failed: {e!r}\n")
            return self.error(conn, "could not start the session", "", rid)
        if conn.dead:
            return
        out = {"type": "session_created", "session": s.id}
        conn.send({**out, "id": rid} if rid is not None else out)
        self._attach(conn, s, None, None)

    def _attach(self, conn: Connection, s: Session, since: int | None, rid: str | None) -> None:
        s.clients.add(conn)
        conn.sessions.add(s.id)
        s.replay_to(conn, since, rid)

    async def _drive(self, conn: Connection, s: Session, kind: str, msg: dict[str, Any], rid: str | None) -> None:
        b = s.backend
        if kind == "send":
            text = str(msg["text"])
            s.busy, s.turn_owner = True, conn.principal.id
            s.publish(ev.UserSaid(conn.principal.id, text, conn.client))
            await b.send(text)
        elif kind == "interrupt":
            open_asks = list(s.asks)
            await b.interrupt()
            for aid in open_asks:                                        # interrupt denies everything waiting
                if aid in s.asks:
                    s.publish(ev.PermissionResolved(aid, "deny", conn.principal.id))
        elif kind == "answer":
            decision = msg.get("decision")
            if decision not in DECISIONS:
                return self.error(conn, f"decision must be one of {', '.join(DECISIONS)}", s.id, rid)
            reply = ev.PermissionReply(decision, str(msg.get("reason", "")))
            if b.answer(str(msg["ask_id"]), reply):
                s.publish(ev.PermissionResolved(str(msg["ask_id"]), reply.decision, conn.principal.id))
            else:
                self.error(conn, f"ask {msg['ask_id']!r} is already resolved or unknown", s.id, rid)
        elif kind == "set_model":
            await b.set_model(msg.get("model"))
            s.model = msg.get("model") or s.model
        elif kind == "set_permission_mode":
            await b.set_permission_mode(str(msg["mode"]))
            s.mode = str(msg["mode"])
        elif kind == "set_trust":
            b.set_trust(msg.get("profile"))
            s.trust = msg.get("profile") or s.trust
        elif kind == "stop_task":
            await b.stop_task(str(msg["task_id"]))

    async def shutdown(self) -> None:
        await self.manager.close_all()
        for c in list(self.conns):                               # else wait_closed() waits on them (3.12+)
            await c.close()


# ── transports ──────────────────────────────────────────────────────────────

class Listener(Protocol):
    """A transport. It authenticates and hands the server (reader, writer, Principal)."""
    async def start(self, on_connect: Callable[[asyncio.StreamReader, asyncio.StreamWriter, Principal], Awaitable[None]]) -> None: ...
    async def close(self) -> None: ...


def default_socket() -> Path:
    run = os.environ.get("XDG_RUNTIME_DIR")
    return (Path(run) / "rabble" if run else Path.home() / ".local" / "state" / "rabble") / "engine.sock"


class UnixListener:
    """Local transport. The principal is the connecting OS user (SO_PEERCRED); the socket is 0600.

    Starting is serialised by an exclusive flock on `<name>.lock` beside the socket, held for the
    listener's life: whoever holds it owns the path, so a socket found while holding it is stale
    and two engines racing cannot both claim it. The directory must be ours (or a sticky one such
    as /tmp); close() unlinks only the inode this listener bound."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._server: asyncio.AbstractServer | None = None
        self._lock_fd: int | None = None
        self._ino: tuple[int, int] | None = None

    @property
    def lock_path(self) -> Path:
        return self.path.with_suffix(".lock")

    def _check_dir(self) -> None:
        d = self.path.parent
        d.mkdir(parents=True, exist_ok=True, mode=0o700)
        st = d.stat()
        if st.st_uid == os.getuid():
            if st.st_mode & 0o077:
                d.chmod(0o700)                                   # ours: make it private
        elif not (st.st_mode & stat.S_ISVTX):
            raise RuntimeError(f"{d} is not owned by this user; refusing to put the engine socket there")

    def _lock(self) -> None:
        fd = os.open(self.lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            raise RuntimeError(f"an engine is already listening on {self.path}") from None
        self._lock_fd = fd

    def _unlock(self) -> None:
        if self._lock_fd is not None:
            os.close(self._lock_fd)                              # closing releases the flock
            self._lock_fd = None

    async def start(self, on_connect) -> None:
        self._check_dir()
        self._lock()
        try:
            self.path.unlink(missing_ok=True)                    # we hold the lock, so whatever is here is dead

            async def accept(reader, writer):
                sock = writer.get_extra_info("socket")
                try:
                    _pid, uid, _gid = struct.unpack("3i", sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")))
                except OSError:
                    writer.close()                               # no verifiable identity, no service
                    return
                await on_connect(reader, writer, Principal(f"local:{uid}", "local"))

            old = os.umask(0o177)
            try:
                self._server = await asyncio.start_unix_server(accept, str(self.path), limit=UP_LINE_LIMIT)
            finally:
                os.umask(old)
            self.path.chmod(0o600)
            st = self.path.stat()
            self._ino = (st.st_dev, st.st_ino)
        except BaseException:
            self._unlock()
            raise

    async def close(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        if self._ino is not None:
            try:
                st = self.path.stat()
                if (st.st_dev, st.st_ino) == self._ino:          # never unlink a successor's socket
                    self.path.unlink()
            except FileNotFoundError:
                pass
            self._ino = None
        self._unlock()


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
                                 trust_override=p.get("trust"),
                                 # a project engine.json can name env and MCP commands: only a local principal's cwd is trusted with it
                                 engine=EngineConfig.load(cwd if p.get("principal_kind") == "local" else None))
    return make


async def serve(listener: Listener, factory: BackendFactory, engine: str = "", policy: Policy = local_policy) -> Server:
    server = Server(SessionManager(factory), engine, policy)
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
    server = await serve(listener, factory, engine="replay" if a.replay else "claude-code")
    sys.stderr.write(f"orchestra.serve: listening on {a.socket}\n")
    stop = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        asyncio.get_running_loop().add_signal_handler(sig, stop.set)
    try:
        await stop.wait()
    finally:
        await server.shutdown()                                  # close every backend: no CLI children left behind
        await listener.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
