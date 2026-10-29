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
from orchestra import park, trust, usage  # noqa: E402
from orchestra.entropy import Entropy  # noqa: E402
from ui.prefs import MODELS, MODES as PREF_MODES, Settings, resolve_model  # noqa: E402
from ui.widgets.composer import Composer, Prompt, copy_to_system  # noqa: E402
from ui.widgets.divider import Divider  # noqa: E402
from ui.widgets.header import Header  # noqa: E402
from ui.widgets.transcript import Transcript  # noqa: E402

MODES = list(PREF_MODES)
QUIT_WINDOW = 2.0
FLASH = 2.5
# dependency order; ui.app last. app-level CSS/BINDINGS and the engine (orchestra/) need a real restart.
RELOAD_MODULES = ("ui.theme", "ui.eyes", "ui.face", "ui.prefs", "ui.widgets.models", "ui.widgets.tool_card",
                  "ui.widgets.permission", "ui.widgets.sessions", "ui.widgets.transcript", "ui.widgets.header",
                  "ui.widgets.composer", "ui.widgets.divider", "ui.app")
USAGE_EVERY = 30.0   # seconds between reads of the OS tracker's cache
HELP = {
    "/model": "pick the model (or /model opus|sonnet|haiku|fable|default|<id>)",
    "/crew": "organs on/off: /crew on|off (off = solo, no agents, cheapest)",
    "/trust": "auto-approve level: /trust strict|standard|trusted|auto (auto = earned from session entropy)",
    "/mode": "permission mode: /mode default|acceptEdits|plan|auto (Shift+Tab cycles)",
    "/status": "model, crew, trust and mode at a glance",
    "/park": "hand off to a fresh session: /park [now|continue] | auto|ask|off | continue on|off (parks itself at 80% context)",
    "/resume": "pick an earlier session",
    "/clear": "start a fresh session",
    "/reload": "rebuild the UI from the code on disk; the agent session carries on (idle only)",
    "/copy": "copy the last reply to the clipboard (Ctrl-C copies a mouse selection; Ctrl-V pastes)",
    "/face": "show the full face",
    "/help": "this list",
    "/quit": "leave (Ctrl-C twice also works)",
}
COMMANDS = set(HELP)


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
                 mode: str = "default", resume: str | None = None, continue_: bool = False,
                 settings: Settings | None = None):
        super().__init__()
        self.make_backend = backend_factory
        self.cwd = cwd
        # one object shared with the backend factory: edits here apply at the next (re)start
        self.settings = settings or Settings(model=model, mode=mode)
        self.model_name = self.settings.model
        self.mode = self.settings.mode if self.settings.mode in MODES else "default"
        self.session_id: str | None = None
        self.entropy: ev.EntropyChanged | None = None  # latest report from the engine
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
        self._turn_open = False  # a turn we sent is still running
        self._bloat = park.Bloat()  # context size and fill rate, per turn
        self._parking: tuple[Path, float] | None = None  # (handoff path, started) while the lead writes it
        self._park_pending = self._park_warned = self._park_blocked = self._park_asked = False
        self._seed: str | None = None  # first message for a session parked from a handoff
        self._continue_once = False
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
        self._sync_header()
        self.transcript = Transcript(self.organ_color)
        self.prompt = Prompt(self.cwd, sorted(COMMANDS))
        yield self.header
        yield Divider()
        yield self.transcript
        yield self.prompt

    @property
    def composer(self) -> Composer:
        return self.prompt.composer

    async def on_mount(self) -> None:
        self._poll_usage()
        self._usage_timer = self.set_interval(USAGE_EVERY, self._poll_usage)
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
                try:
                    await self._handle(event)
                except Exception as e:  # one event the view cannot draw must not end the session
                    await self.transcript.notice(f"view: {type(event).__name__}: {e}", error=True)
        except Exception as e:  # the session is over; say so rather than crash the face
            await self.transcript.notice(f"engine: {e}", error=True)

    # terminal focus: the view must survive switching away and back
    def on_app_blur(self, event) -> None:
        self.transcript.remember_view()

    def on_app_focus(self, event) -> None:
        self.transcript.restore_view()

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
                self.session_id = e.session_id
                h.model = e.model or h.model
                self._sync_header()
                if self._seed:
                    seed, self._seed = self._seed, None
                    self._turn_open = True
                    if await self._engine("send", self.backend.send, seed):
                        await t.thinking()
            case ev.SessionResumed():
                self.session_id = e.session_id
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
            case ev.EntropyChanged():
                self.entropy = e
                setter = getattr(self.face, "set_signal", None)
                if setter:
                    setter(e.base, e.stability)
                self._sync_header()
                if e.reason and e.band not in ("STABLE", "NOMINAL"):
                    await t.notice(f"entropy {e.band.lower()} ({e.score:.2f}): {e.reason}. Verify critical decisions."
                                   f" trust {e.trust}", error=e.band in ("DEGRADED", "UNSTABLE"))
            case ev.Usage():
                if e.cost is not None:
                    h.cost = e.cost
                if e.context_pct is not None:
                    h.context_pct = e.context_pct
                h.refresh_info()
                if e.context_pct is not None:
                    self._bloat.add(e.tokens, e.context_pct)
                    h.park_eta = self._bloat.eta() if self.settings.park != "off" else None
                    h.refresh_info()
                    await self._check_park(e.context_pct)
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
                    self._turn_open = False
                    if self._parking:
                        # the restart cancels this worker's group, so it runs in its own
                        self.run_worker(self._finish_park(), group="park")
                    elif self._park_pending:
                        await self._begin_park()
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
        elif self._parking:
            await self.transcript.notice("parking: the handoff is being written, then a fresh session starts", error=True)
        else:
            await self.transcript.user(text)
            self._turn_open = True
            if await self._engine("send", self.backend.send, text):
                await self.transcript.thinking()
        self._set_mood()

    async def _command(self, word: str, arg: str) -> None:
        t = self.transcript
        if word == "/quit":
            self.exit()
        elif word == "/model":
            if arg:
                await self._set_model(resolve_model(arg))
            else:
                from ui.widgets.models import ModelPicker

                async def picked(choice) -> None:
                    if choice is not None:
                        await self._set_model(choice[0])

                self.push_screen(ModelPicker(self.settings.model), lambda c: self.run_worker(picked(c)))
        elif word == "/crew":
            await self._set_crew(arg.lower())
        elif word == "/trust":
            await self._set_trust(arg.lower())
        elif word == "/park":
            await self._park_command(arg.lower().split())
        elif word == "/status":
            await t.notice(self._status())
        elif word == "/help":
            await t.notice("\n".join(f"{c:8} {d}" for c, d in HELP.items()) +
                           "\nOther /commands (/compact, /init, skills...) go to Claude Code as typed.")
        elif word == "/mode":
            if arg not in MODES:
                return await t.notice("usage: /mode " + "|".join(MODES), error=True)
            await self._set_mode(arg)
        elif word == "/reload":
            await self._reload_ui()
        elif word == "/copy":
            await self._copy(self.transcript.last_reply, "last reply")
        elif word == "/face":
            face_cls = _optional("ui.face", "FaceWidget")
            w = face_cls("full") if face_cls else Static("RaBbLE")
            if self.entropy and hasattr(w, "set_signal"):
                w.set_signal(self.entropy.base, self.entropy.stability)
            t.transient.append(w)
            await t.mount(w)
            self._sync_face()
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

    async def _engine(self, what: str, fn, *args) -> bool:
        """Every face → engine call goes through here. The client is None until start()
        finishes and gone once the engine dies; a raise in a message handler would exit
        the app and lose the transcript, so it becomes a notice instead."""
        if self.backend is None:
            await self.transcript.notice(f"{what}: engine not running (/clear restarts it)", error=True)
            return False
        try:
            result = fn(*args)
            if hasattr(result, "__await__"):
                await result
            return True
        except Exception as e:
            await self.transcript.notice(f"{what} failed: {e} (/clear restarts the engine)", error=True)
            return False

    async def _restart(self, resume: str | None, cont: bool, clear: bool = False, keep: bool = False,
                       parked: Path | None = None) -> None:
        self.workers.cancel_group(self, "engine")
        old = self.backend
        self.settings.carry = getattr(old, "entropy", None) if keep else None
        if parked:
            # a fresh score at the model's baseline; the backend marks it as an explicit (cheap) handoff
            self.settings.carry = Entropy(self.settings.model)
        if old is not None:
            try:
                await old.close()
            except Exception:
                pass
        self._asks.clear()
        self._busy_ids.clear()
        self._streaming = False
        self._turn_open = False
        self._parking = None
        self._park_pending = self._park_warned = self._park_blocked = self._park_asked = False
        if not keep:
            self._bloat = park.Bloat()
            self.header.context_pct = None
            self.header.park_eta = None
        if clear or not resume:
            self.entropy = None  # a new session starts from its model's baseline
        if clear or parked or (resume and not keep):
            await self.transcript.clear_all()
        if parked:
            keep_going = self.settings.park_continue or self._continue_once
            self._continue_once = False
            self._seed = park.fresh_prompt(parked, keep_going)
            await self.transcript.notice(f"parked; fresh session starting from {parked}"
                                         + (" and carrying on" if keep_going else " and waiting for you"))
        elif not keep:
            await self.transcript.notice("new session" if clear else f"resuming {resume[:8]}")
        self.backend = self.make_backend(resume, cont)
        self._pump(self.backend)

    # parking: hand off to a fresh session before the context fills (orchestra/park.py)
    async def _check_park(self, pct: float) -> None:
        mode = self.settings.park
        if mode == "off" or self._parking or self._park_blocked:
            return
        why = self._bloat.reason()
        if why:
            if mode == "ask":
                if not self._park_asked:
                    self._park_asked = True
                    await self.transcript.notice(f"{why}: /park hands off to a fresh session")
            elif self._turn_open:
                self._park_pending = True
            else:
                await self._begin_park(pct, why)
        elif pct >= park.WARN_AT and not self._park_warned:
            self._park_warned = True
            await self.transcript.notice(
                f"context {pct:.0f}%: parking at {park.PARK_AT:.0f}% (handoff, then a fresh session; /park off to stop)")

    async def _begin_park(self, pct: float | None = None, why: str | None = None) -> None:
        pct = pct if pct is not None else (self.header.context_pct or 0.0)
        self._park_pending = False
        path = park.handoff_path(self.cwd, self.session_id)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            self._park_blocked = True
            return await self.transcript.notice(f"park failed: cannot use {path.parent}: {e}", error=True)
        self._parking = (path, time.time())
        self._turn_open = True
        await self.transcript.notice(f"{why or f'context {pct:.0f}%'}: parking. Handoff and session log first, then a fresh session")
        if await self._engine("park", self.backend.send, park.park_prompt(path, pct)):
            await self.transcript.thinking()
        else:
            self._parking = None
            self._park_blocked = True

    async def _finish_park(self) -> None:
        path, since = self._parking or (None, 0.0)
        self._parking = None
        if path is None:
            return
        why = park.check_handoff(path, since)
        if why:
            self._park_blocked = True
            return await self.transcript.notice(
                f"park failed: {why}. Staying in this session; /park tries again", error=True)
        await self._restart(None, False, clear=True, parked=path)

    async def _park_command(self, words: list[str]) -> None:
        t = self.transcript
        s = self.settings
        if not words or words[0] == "status":
            return await t.notice(f"park {s.park} at {park.PARK_AT:.0f}% context, new session "
                                  f"{'carries on' if s.park_continue else 'waits for you'}; "
                                  f"/park now | /park continue | /park auto|ask|off | /park continue on|off")
        if words[0] in park.MODES:
            s.park = words[0]
            s.save()
            self._park_blocked = False
            return await t.notice(f"park {s.park}")
        if words[0] == "continue" and len(words) > 1 and words[1] in ("on", "off"):
            s.park_continue = words[1] == "on"
            s.save()
            return await t.notice(f"after a park the fresh session {'carries on' if s.park_continue else 'waits for you'}")
        if words[0] in ("now", "continue"):
            if self._turn_open or self._parking:
                return await t.notice("a turn is running; park when it finishes", error=True)
            self._continue_once = "continue" in words
            self._park_blocked = False
            return await self._begin_park()
        await t.notice("usage: /park [now|continue] | auto|ask|off | continue on|off", error=True)

    async def _set_mode(self, mode: str) -> None:
        if not await self._engine("mode", self.backend.set_permission_mode, mode):
            return
        self.mode = self.settings.mode = mode
        self.settings.save()
        self._sync_header()

    # settings
    def _model_for_entropy(self) -> str | None:
        return self.header.model if self.header.model != "default" else self.settings.model

    def trust_profile(self) -> str:
        if self.settings.trust in trust.PROFILES:
            return self.settings.trust
        return self.entropy.trust if self.entropy else trust.profile_for(self._model_for_entropy())

    def _poll_usage(self) -> None:
        self.header.usage = usage.read()
        self.header.refresh_info()

    def _sync_header(self) -> None:
        self.header.mode = self.mode
        self.header.crew = self.settings.crew
        self.header.trust = self.trust_profile()
        if self.entropy:
            self.header.entropy = (self.entropy.score, self.entropy.band)
            self.header.capability, self.header.categories = self.entropy.capability, self.entropy.categories
            self.header.local = self.entropy.local
            self.header.stability = self.entropy.stability
        else:  # before the engine reports: the model's own numbers
            base = Entropy(self._model_for_entropy())
            self.header.capability, self.header.categories, self.header.local = base.capability(), base.capabilities(), base.local
        self._sync_face()
        self.header.refresh_info()

    def _sync_face(self) -> None:
        """The face's aura follows the model's provider."""
        from_ui = _optional("ui.face", "provider_of")
        if from_ui is None:
            return
        provider = from_ui(self._model_for_entropy(), self.header.local)
        faces = [self.face]
        transcript = getattr(self, "transcript", None)
        if transcript is not None and transcript.is_attached:
            faces += list(transcript.query("FaceWidget"))
        for w in faces:
            setter = getattr(w, "set_provider", None)
            if setter:
                setter(provider)

    def _status(self) -> str:
        ent = ""
        if self.entropy:
            c = self.entropy.categories
            ent = (f"{self.entropy.band.lower()} · stability {self.entropy.stability} · capability {self.entropy.capability}"
                   f" (coding {c.get('coding', '-')} tools {c.get('tools', '-')} recall {c.get('recall', '-')}"
                   f"{', local' if self.entropy.local else ''}) · ")
        u = self.header.usage
        use = "" if u is None or not (u.five_hour or u.week) else (
            "usage " + " ".join(f"{n} {w.pct:.0f}% (resets {usage.fmt_remaining(w.resets_in())})"
                                for n, w in (("5h", u.five_hour), ("week", u.week)) if w)
            + (" [stale]" if u.stale else "") + " · ")
        return (f"model {self.header.model} · {ent}{use}{self._bloat.summary()} · crew {'on' if self.settings.crew else 'off'} · "
                f"trust {self.trust_profile()}{' (set)' if self.settings.trust else ' (from entropy)'} · mode {self.mode}")

    async def _set_model(self, value: str | None) -> None:
        if not await self._engine("model", self.backend.set_model, value):
            return
        self.settings.model = value
        self.settings.save()
        self.header.model = value or "default"
        self._sync_header()
        await self.transcript.notice(f"model {value or 'default'} · trust {self.trust_profile()}")

    async def _set_trust(self, arg: str) -> None:
        if arg not in (*trust.PROFILES, "auto"):
            return await self.transcript.notice(
                f"trust is {self.trust_profile()}; usage: /trust {'|'.join(trust.PROFILES)}|auto", error=arg != "")
        self.settings.trust = None if arg == "auto" else arg
        self.settings.save()
        if self.backend is not None:
            self.backend.set_trust(self.settings.trust)
        self._sync_header()
        await self.transcript.notice(f"trust {self.trust_profile()}" + (" (from entropy)" if arg == "auto" else ""))

    async def _set_crew(self, arg: str) -> None:
        if arg not in ("on", "off"):
            return await self.transcript.notice(
                f"crew is {'on' if self.settings.crew else 'off'}; usage: /crew on|off", error=arg != "")
        want = arg == "on"
        if want == self.settings.crew:
            return await self.transcript.notice(f"crew already {arg}")
        self.settings.crew = want
        self.settings.save()
        self._sync_header()
        # agents and the roster live in the engine's start options, so the engine restarts on the same session
        await self._restart(self.session_id, False, keep=True)
        await self.transcript.notice(f"crew {arg}" + (" · organs cost a full context each, use for parallel work" if want else " · solo"))

    async def on_composer_command(self, m: Composer.Command) -> None:
        name = m.name
        if name == "interrupt":
            self._asks.clear()
            await self._engine("interrupt", self.backend.interrupt)
            await self.transcript.stop_thinking()
        elif name == "cycle_mode":
            await self._set_mode(MODES[(MODES.index(self.mode) + 1) % len(MODES)])
        elif name == "copy":
            await self._copy(self.screen.get_selected_text() or "", "selection")
            self.screen.clear_selection()
        elif name == "ctrl_d":
            self.exit()
        elif name == "ctrl_c":
            now = time.monotonic()
            if now - self._last_ctrl_c <= QUIT_WINDOW:
                self.exit()
            else:
                self._last_ctrl_c = now
                await self.transcript.notice("press Ctrl-C again to quit")

    async def _reload_ui(self) -> None:
        """Re-import ui/*, swap this app to the new class and remount the widgets. The backend object (and so the
        Claude Code session) is never touched; the view is rebuilt from the transcript's record."""
        t = self.transcript
        if self._turn_open or self._asks or self._ask_open or self._parking:
            return await t.notice("reload when the turn is done (nothing mid-stream or awaiting approval)", error=True)
        for path in sorted(Path(__file__).parent.rglob("*.py")):
            try:
                compile(path.read_text(encoding="utf-8"), str(path), "exec")
            except SyntaxError as e:
                return await t.notice(f"reload stopped, nothing changed: {path.name}:{e.lineno} {e.msg}", error=True)
        try:
            for name in RELOAD_MODULES:
                if name in sys.modules:
                    importlib.reload(sys.modules[name])
        except Exception as e:
            return await t.notice(f"reload failed in {name}: {e}. Some modules may be half-updated: restart rabble if the UI misbehaves", error=True)
        self.__class__ = sys.modules["ui.app"].RabbleApp
        await self._rebuild_ui()

    async def _rebuild_ui(self) -> None:
        keep = (self.header.export_state(), list(self.transcript.shown), self.composer.text, list(self.composer._history))
        for w in (self.header, self.transcript, self.prompt):
            await w.remove()
        palette = _optional("ui.theme", "load_palette")
        self.palette = palette() if palette else {}
        self.organ_color = _optional("ui.theme", "organ_color") or _fallback_color
        theme = _optional("ui.theme", "rabble_theme")
        if theme:
            self.register_theme(theme())
        widgets = list(self.compose())
        await self.mount(*widgets)
        state, log, draft, history = keep
        self.header.import_state(state)
        self._sync_header()
        await self.transcript.replay(log)
        self.composer._history = history
        self.composer.load_text(draft)
        self._usage_timer.stop()
        self._usage_timer = self.set_interval(USAGE_EVERY, self._poll_usage)
        self.composer.focus()
        await self.transcript.notice("UI reloaded; the session carries on")

    async def _copy(self, text: str, what: str) -> None:
        if not text:
            return await self.transcript.notice(f"nothing to copy ({what})", error=True)
        self.copy_to_clipboard(text)   # OSC 52: also reaches the local clipboard over SSH
        copy_to_system(text)
        await self.transcript.notice(f"copied {what} ({len(text)} chars)")

    def action_toggle_tools(self) -> None:
        self.transcript.toggle_cards()

    BINDINGS = [("ctrl+o", "toggle_tools", "tools")]

    async def on_unmount(self) -> None:
        if self.backend is not None:
            try:
                await self.backend.close()
            except Exception:
                pass
