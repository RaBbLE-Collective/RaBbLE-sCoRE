"""RaBbLE-UI: the Textual face over a Backend (orchestra/). It draws orchestra events
and sends the Observer's input back; it never imports the SDK.

Optional pieces (theme, face, permission modal, session picker) load through
_optional() so the app still runs while any of them is missing.
"""

import importlib
import sys
import time
from collections import deque
from pathlib import Path

from textual import work
from textual.app import App
from textual.widgets import Static

SCORE_ROOT = Path(__file__).resolve().parent.parent
for _p in (SCORE_ROOT, SCORE_ROOT / "crew"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from orchestra import events as ev  # noqa: E402
from ui.widgets.composer import Composer, Prompt  # noqa: E402
from ui.widgets.header import Header  # noqa: E402
from ui.widgets.transcript import Transcript  # noqa: E402

MODES = ["default", "acceptEdits", "plan"]
QUIT_WINDOW = 2.0
FLASH = 2.5
COMMANDS = {"/model", "/mode", "/resume", "/clear", "/face", "/quit"}


def _optional(module: str, name: str):
    try:
        return getattr(importlib.import_module(module), name)
    except Exception:
        return None


def _fallback_color(slug: str) -> str:
    return "cyan"


class RabbleApp(App):
    TITLE = "RaBbLE"
    CSS = """
    Screen { background: $background; color: $foreground; }
    """

    def __init__(self, backend_factory, *, cwd: str, model: str | None = None,
                 mode: str = "default", resume: str | None = None, continue_: bool = False):
        super().__init__()
        self.make_backend = backend_factory
        self.cwd = cwd
        self.model_name = model
        self.mode = mode if mode in MODES else "default"
        self._resume, self._continue = resume, continue_
        self.backend = None
        self._asks: deque[ev.PermissionAsk] = deque()
        self._ask_open = False
        self._last_ctrl_c = 0.0
        self._flash: tuple[str, float] | None = None
        self._streaming = False
        self._busy_ids: set[str] = set()
        self._typing = False
        self._closing = False
        palette = _optional("ui.theme", "load_palette")
        self.palette = palette() if palette else {}
        self.organ_color = _optional("ui.theme", "organ_color") or _fallback_color
        theme = _optional("ui.theme", "rabble_theme")
        if theme:
            self.register_theme(theme())
            self.theme = "rabble"

    # layout
    def compose(self):
        face_cls = _optional("ui.face", "FaceWidget")
        self.face = face_cls("mini") if face_cls else Static("RaBbLE")
        self.header = Header(self.face, self.palette, model=self.model_name or "", cwd=self.cwd, mode=self.mode)
        self.transcript = Transcript(self.organ_color)
        self.prompt = Prompt(self.cwd)
        yield self.header
        yield self.transcript
        yield self.prompt

    @property
    def composer(self) -> Composer:
        return self.prompt.composer

    async def on_mount(self) -> None:
        self.composer.focus()
        if Path(self.cwd).resolve().is_relative_to(SCORE_ROOT):
            await self.transcript.notice(
                "inside RaBbLE-sCoRE, whose settings block Bash and teammates; start from the Collective root", error=True)
        self._boot()

    def _boot(self) -> None:
        self.backend = self.make_backend(self._resume, self._continue)
        self._pump(self.backend)

    @work(exclusive=True, group="engine")
    async def _pump(self, backend) -> None:
        try:
            await backend.start()
            async for event in backend.events():
                await self._handle(event)
        except Exception as e:  # the session is over; say so rather than crash the face
            await self.transcript.notice(f"engine: {e}", error=True)

    # mood
    def _mood(self) -> str:
        if self._flash and time.monotonic() < self._flash[1]:
            return self._flash[0]
        if self._streaming:
            return "streaming"
        if self._busy_ids:
            return "working"
        return "typing" if self.composer.text else "idle"

    def _set_mood(self) -> None:
        self.header.set_mood(self._mood())

    def _flash_mood(self, mood: str) -> None:
        self._flash = (mood, time.monotonic() + FLASH)
        self._set_mood()
        self.set_timer(FLASH + 0.05, self._set_mood)

    def on_text_area_changed(self, event) -> None:
        self._set_mood()

    # events
    async def _handle(self, e: ev.Event) -> None:
        t, h = self.transcript, self.header
        match e:
            case ev.SessionStarted():
                h.model = e.model or h.model
                h.refresh_info()
            case ev.SessionResumed():
                await t.notice(f"resumed session {e.session_id[:8]}")
            case ev.TextDelta():
                self._streaming = True
                await t.delta(e.organ, e.text)
            case ev.TextBlock():
                self._streaming = False
                await t.block(e.organ, e.text)
            case ev.Thinking():
                await t.thinking()
            case ev.ToolCall():
                self._streaming = False
                self._busy_ids.add(e.id)
                await t.tool_call(e.organ, e.id, e.name, e.input)
            case ev.ToolResult():
                self._busy_ids.discard(e.id)
                t.tool_result(e.id, e.ok, e.summary)
            case ev.PermissionAsk():
                self._asks.append(e)
                self._next_ask()
            case ev.OrganStarted():
                self._busy_ids.add("organ:" + e.organ)
                await t.notice(f"{e.organ} started: {e.brief}")
            case ev.OrganDone():
                self._busy_ids.discard("organ:" + e.organ)
                await t.notice(f"{e.organ} {e.status}", error=e.status != "completed")
            case ev.Usage():
                if e.cost is not None:
                    h.cost = e.cost
                if e.context_pct is not None:
                    h.context_pct = e.context_pct
                h.refresh_info()
            case ev.RateLimited():
                h.limit, h.limit_until = e.status, e.until
                h.refresh_info()
                if e.status != "allowed":
                    await t.notice(f"rate limit {e.status}", error=e.status == "rejected")
            case ev.TurnDone():
                self._streaming = False
                await t.stop_thinking()
                t.end_turn()
                if e.origin == "observer":
                    self._busy_ids.clear()
                else:
                    await t.notice("background turn finished")
            case ev.Error():
                self._streaming = False
                await t.notice(e.text, error=True)
                self._flash_mood("error")
        self._set_mood()

    # permissions
    def _next_ask(self) -> None:
        if self._ask_open or not self._asks:
            return
        ask = self._asks.popleft()
        screen = _optional("ui.widgets.permission", "PermissionScreen")
        if screen is None:
            self.backend.answer(ask.id, ev.PermissionReply("deny", "permission prompt unavailable"))
            self.run_worker(self.transcript.notice(f"denied {ask.tool}: permission prompt unavailable", error=True))
            self._flash_mood("denied")
            return self._next_ask()
        self._ask_open = True

        def done(reply) -> None:
            self._ask_open = False
            reply = reply or ev.PermissionReply("deny", "dismissed")
            self.backend.answer(ask.id, reply)
            if reply.decision == "deny":
                self._flash_mood("denied")
            self.composer.focus()
            self._next_ask()

        self.push_screen(screen(ask), done)

    # input
    async def on_composer_submit(self, m: Composer.Submit) -> None:
        await self.transcript.drop_transient()
        text = m.text
        word = text.split(None, 1)[0]
        if word in COMMANDS:
            await self._command(word, text[len(word):].strip())
        else:
            await self.transcript.user(text)
            await self.backend.send(text)
            await self.transcript.thinking()
        self._set_mood()

    async def _command(self, word: str, arg: str) -> None:
        t = self.transcript
        if word == "/quit":
            self.exit()
        elif word == "/model":
            if not arg:
                return await t.notice(f"model: {self.header.model}")
            await self.backend.set_model(arg)
            self.header.model = arg
            self.header.refresh_info()
            await t.notice(f"model set to {arg}")
        elif word == "/mode":
            if arg not in MODES:
                return await t.notice("usage: /mode " + "|".join(MODES), error=True)
            await self._set_mode(arg)
        elif word == "/face":
            face_cls = _optional("ui.face", "FaceWidget")
            w = face_cls("full") if face_cls else Static("RaBbLE")
            t.transient.append(w)
            await t.mount(w)
        elif word == "/clear":
            await self._restart(None, False, clear=True)
        elif word == "/resume":
            picker = _optional("ui.widgets.sessions", "SessionPicker")
            if picker is None:
                return await t.notice("/resume unavailable", error=True)

            async def chosen(sid) -> None:
                if sid:
                    await self._restart(sid, False)

            self.push_screen(picker(self.cwd), lambda sid: self.run_worker(chosen(sid)))

    async def _restart(self, resume: str | None, cont: bool, clear: bool = False) -> None:
        self.workers.cancel_group(self, "engine")
        old = self.backend
        if old is not None:
            try:
                await old.close()
            except Exception:
                pass
        self._asks.clear()
        self._busy_ids.clear()
        self._streaming = False
        if clear or resume:
            await self.transcript.clear_all()
        await self.transcript.notice("new session" if clear else f"resuming {resume[:8]}")
        self.backend = self.make_backend(resume, cont)
        self._pump(self.backend)

    async def _set_mode(self, mode: str) -> None:
        await self.backend.set_permission_mode(mode)
        self.mode = mode
        self.header.mode = mode
        self.header.refresh_info()

    async def on_composer_command(self, m: Composer.Command) -> None:
        name = m.name
        if name == "interrupt":
            self._asks.clear()
            await self.backend.interrupt()
            await self.transcript.stop_thinking()
        elif name == "cycle_mode":
            await self._set_mode(MODES[(MODES.index(self.mode) + 1) % len(MODES)])
        elif name == "ctrl_d":
            self.exit()
        elif name == "ctrl_c":
            now = time.monotonic()
            if now - self._last_ctrl_c <= QUIT_WINDOW:
                self.exit()
            else:
                self._last_ctrl_c = now
                await self.transcript.notice("press Ctrl-C again to quit")

    def action_toggle_tools(self) -> None:
        self.transcript.toggle_cards()

    BINDINGS = [("ctrl+o", "toggle_tools", "tools")]

    async def on_unmount(self) -> None:
        if self.backend is not None:
            try:
                await self.backend.close()
            except Exception:
                pass
