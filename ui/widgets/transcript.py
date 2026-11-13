"""The transcript: user lines, streamed lead Markdown, organ text, tool cards, notices.

Lead text arrives as TextDeltas and then a TextBlock of the same text; the block
REPLACES the streamed Markdown so nothing shows twice.
"""

import math
import os
import time
import traceback

from rich.text import Text
from textual.containers import VerticalScroll
from textual.widgets import Markdown, Static

from orchestra.events import LEAD
from ui.widgets.tool_card import ToolCard


class Thinking(Static):
    DEFAULT_CSS = "Thinking { height: 1; padding: 0 1; color: $text-muted; }"

    def on_mount(self) -> None:
        self._n = 0
        self.set_interval(0.4, self._pulse)
        self._pulse()

    def _pulse(self) -> None:
        self._n += 1
        self.update(Text("thinking" + "." * (self._n % 4), style="italic"))


class Transcript(VerticalScroll):
    DEFAULT_CSS = """
    Transcript { height: 1fr; padding: 0 1; scrollbar-size-vertical: 1; }
    Transcript Markdown { margin: 0; padding: 0; background: transparent; }
    Transcript .user { color: $secondary; margin-top: 1; }
    Transcript .organ { margin-top: 1; }
    Transcript .notice { color: $text-muted; }
    Transcript .error { color: $error; }
    """
    can_focus = False

    def __init__(self, organ_color, **kw):
        super().__init__(**kw)
        self._organ_color = organ_color
        self._live: Markdown | None = None
        self._buf = ""                 # text of the reply being streamed
        self.last_reply = ""           # last finished lead reply, for /copy
        self.shown: list[tuple] = []      # what was shown, so /reload can rebuild the view
        self._thinking: Thinking | None = None
        self._cards: dict[str, ToolCard] = {}
        self.transient: list = []
        self._following = True  # changes only when the reader scrolls, never from layout churn
        self._blurred = False
        self._refocused_at = 0.0

    def on_mount(self) -> None:
        self.anchor()
        self.watch(self, "virtual_size", self._regrew)

    # Textual's anchor releases on any wheel/key scroll and only re-arms at the exact bottom, so a
    # reader who ends a line or two short stops following, and a Markdown update() that collapses
    # then regrows leaves the view stranded mid-content. Follow whenever the view was at (or near) the bottom.
    FOLLOW_SLACK = 3

    # a drop to the top while blurred, or just after refocus, with plenty of content is a layout
    # stranding, not the reader (nobody scrolls a window they are not in)
    REFOCUS_GRACE = 2.0

    # Wheel notches arrive in bursts. Restarting an eased animation per notch begins every one at full speed, which is
    # the ratchet. Instead notches only move a target, and ONE 120 Hz glide closes a fixed fraction of the remaining
    # distance per frame (exponential approach): velocity is continuous across notches and settles without a hard stop.
    WHEEL_LINES = 3
    GLIDE_RATE = 16.0        # 1/s; time constant ~60 ms
    GLIDE_FPS = 120          # needs TEXTUAL_FPS >= this (set in ui/__main__.py), else frames are dropped
    GLIDE_MIN_SPEED = 45.0   # lines/s: the exponential tail would crawl a line per few frames, which reads as notches

    _glide_target: float | None = None
    _glide_timer = None
    _glide_at = 0.0

    def _wheel(self, direction: int, event) -> None:
        if event.ctrl or event.shift or not self.allow_vertical_scroll:
            return
        base = self.scroll_y if self._glide_target is None else self._glide_target
        target = min(max(base + direction * self.WHEEL_LINES, 0), self.max_scroll_y)
        event.stop()
        if target == base:
            return
        self._glide_target = target
        if self._glide_timer is None:
            self._glide_at = time.monotonic()
            self._glide_timer = self.set_interval(1 / self.GLIDE_FPS, self._glide)

    def _glide_stop(self) -> None:
        self._glide_target = None
        if self._glide_timer is not None:
            self._glide_timer.stop()
            self._glide_timer = None

    def _glide(self) -> None:
        now = time.monotonic()
        dt, self._glide_at = now - self._glide_at, now
        if self._glide_target is None:
            return self._glide_stop()
        target = min(self._glide_target, self.max_scroll_y)   # content may have shrunk or grown
        gap = target - self.scroll_y
        if abs(gap) < 0.5:
            self.scroll_to(y=target, animate=False, force=True)
            return self._glide_stop()
        step = max(abs(gap) * (1 - math.exp(-dt * self.GLIDE_RATE)), self.GLIDE_MIN_SPEED * dt)
        self.scroll_to(y=self.scroll_y + math.copysign(min(step, abs(gap)), gap), animate=False, force=True)

    def _on_mouse_scroll_down(self, event) -> None:
        self._wheel(1, event)

    def _on_mouse_scroll_up(self, event) -> None:
        self._wheel(-1, event)

    def _near_bottom(self) -> bool:
        return self._following or self.max_scroll_y - self.scroll_y <= self.FOLLOW_SLACK

    def watch_scroll_y(self, old_value: float, new_value: float) -> None:
        super().watch_scroll_y(old_value, new_value)
        stranded = (
            self._following
            and new_value <= 1
            and old_value > 10
            and self.max_scroll_y > self.size.height
            and (self._blurred or time.monotonic() - self._refocused_at < self.REFOCUS_GRACE)
        )
        if os.environ.get("RABBLE_SCROLL_DEBUG") and (abs(new_value - old_value) > 5 or new_value <= 1):
            frames = " < ".join(f"{f.name}:{f.lineno}" for f in traceback.extract_stack()[-7:-1])
            with open("/tmp/rabble-scroll.log", "a") as log:
                log.write(
                    f"{time.strftime('%H:%M:%S')} y {old_value:.0f}->{new_value:.0f} max={self.max_scroll_y:.0f} "
                    f"following={self._following} blurred={self._blurred} stranded={stranded} | {frames}\n"
                )
        if stranded:
            self.call_after_refresh(self.scroll_end, animate=False)
            return
        # the reader's own scroll decides; a layout clamp to the new max keeps us following
        self._following = new_value >= self.max_scroll_y - self.FOLLOW_SLACK + 1

    def _regrew(self, _size) -> None:
        if self._following:
            self.call_after_refresh(self.scroll_end, animate=False)

    def _stick(self, follow: bool) -> None:
        if follow:
            self.call_after_refresh(self.scroll_end, animate=False)

    def remember_view(self) -> None:
        """Window blurred: note where the reader was (at the bottom, or a line number)."""
        self._blurred = True
        self._saved_view = ("end", 0.0) if self._near_bottom() else ("y", self.scroll_y)

    def restore_view(self) -> None:
        """Window refocused: put the reader back. Focus and style refreshes can re-layout the
        Markdown blocks and strand the view at the top, because the released anchor never re-follows."""
        self._blurred = False
        self._refocused_at = time.monotonic()
        saved, self._saved_view = getattr(self, "_saved_view", None), None
        if saved is None:
            return
        kind, y = saved

        def put() -> None:
            if kind == "end":
                self.scroll_end(animate=False)
            else:
                self.scroll_to(y=y, animate=False)
        put()
        self.call_after_refresh(put)
        self.set_timer(0.15, put)  # after any late re-layout

    async def _add(self, widget, force: bool = False):
        follow = force or self._near_bottom()
        await self.mount(widget)
        self._stick(follow)
        return widget

    async def clear_all(self) -> None:
        self._live = self._thinking = None
        self._buf = self.last_reply = ""
        self.shown.clear()
        self._following = True
        self._cards.clear()
        await self.remove_children()

    async def drop_transient(self) -> None:
        for w in self.transient:
            await w.remove()
        self.transient.clear()

    async def user(self, text: str) -> None:
        self.shown.append(("user", text))
        await self._add(Static(Text("› " + text), classes="user"), force=True)  # the Observer spoke: show it

    async def notice(self, text: str, error: bool = False) -> None:
        self.shown.append(("notice", text, error))
        await self._add(Static(Text(text), classes="error" if error else "notice"))

    async def stop_thinking(self) -> None:
        if self._thinking is not None:
            await self._thinking.remove()
            self._thinking = None

    async def thinking(self) -> None:
        if self._thinking is None:
            self._thinking = await self._add(Thinking())

    async def delta(self, organ: str, text: str) -> None:
        await self.stop_thinking()
        if self._live is None:
            self._live = await self._add(Markdown(""))
        follow = self._near_bottom()
        self._buf += text
        self.last_reply = self._buf
        await self._live.append(text)
        self._stick(follow)

    async def block(self, organ: str, text: str) -> None:
        self.shown.append(("block", organ, text))
        await self.stop_thinking()
        if organ == LEAD:
            self.last_reply, self._buf = text, ""
            if self._live is not None:
                follow = self._near_bottom()
                await self._live.update(text)
                self._live = None
                self._stick(follow)
            else:
                await self._add(Markdown(text))
            return
        await self._add(Static(Text(organ, style=f"bold {self._organ_color(organ)}"), classes="organ"))
        await self._add(Markdown(text))

    def end_turn(self) -> None:
        if self._buf:   # streamed with no final block: keep what was shown
            self.shown.append(("block", LEAD, self._buf))
        self._live = None
        self._buf = ""

    async def tool_call(self, organ: str, tool_id: str, name: str, data: dict) -> None:
        self.shown.append(("tool", organ, tool_id, name, data))
        await self.stop_thinking()
        card = ToolCard(tool_id, "" if organ == LEAD else organ, name, data)
        self._cards[tool_id] = card
        await self._add(card)

    def tool_result(self, tool_id: str, ok: bool, summary: str) -> None:
        self.shown.append(("result", tool_id, ok, summary))
        card = self._cards.get(tool_id)
        if card:
            follow = self._near_bottom()
            card.set_result(ok, summary)
            self._stick(follow)

    async def replay(self, entries: list[tuple]) -> None:
        """Show again what an earlier Transcript showed (a rebuilt view after /reload)."""
        for kind, *a in entries:
            if kind == "user":
                await self.user(*a)
            elif kind == "notice":
                await self.notice(*a)
            elif kind == "block":
                await self.block(*a)
            elif kind == "tool":
                await self.tool_call(*a)
            elif kind == "result":
                self.tool_result(*a)
        self.end_turn()

    def toggle_cards(self) -> None:
        cards = list(self._cards.values())
        if cards:
            target = not cards[-1].collapsed
            for c in cards:
                c.collapsed = target
