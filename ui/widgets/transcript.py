"""The transcript: user lines, streamed lead Markdown, organ text, tool cards, notices.

Lead text arrives as TextDeltas and then a TextBlock of the same text; the block
REPLACES the streamed Markdown so nothing shows twice.
"""

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
        self._thinking: Thinking | None = None
        self._cards: dict[str, ToolCard] = {}
        self.transient: list = []

    def on_mount(self) -> None:
        self.anchor()

    async def _add(self, widget):
        await self.mount(widget)
        return widget

    async def clear_all(self) -> None:
        self._live = self._thinking = None
        self._cards.clear()
        await self.remove_children()

    async def drop_transient(self) -> None:
        for w in self.transient:
            await w.remove()
        self.transient.clear()

    async def user(self, text: str) -> None:
        await self._add(Static(Text("› " + text), classes="user"))

    async def notice(self, text: str, error: bool = False) -> None:
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
        await self._live.append(text)

    async def block(self, organ: str, text: str) -> None:
        await self.stop_thinking()
        if organ == LEAD:
            if self._live is not None:
                await self._live.update(text)
                self._live = None
            else:
                await self._add(Markdown(text))
            return
        await self._add(Static(Text(organ, style=f"bold {self._organ_color(organ)}"), classes="organ"))
        await self._add(Markdown(text))

    def end_turn(self) -> None:
        self._live = None

    async def tool_call(self, organ: str, tool_id: str, name: str, data: dict) -> None:
        await self.stop_thinking()
        card = ToolCard(tool_id, "" if organ == LEAD else organ, name, data)
        self._cards[tool_id] = card
        await self._add(card)

    def tool_result(self, tool_id: str, ok: bool, summary: str) -> None:
        card = self._cards.get(tool_id)
        if card:
            card.set_result(ok, summary)

    def toggle_cards(self) -> None:
        cards = list(self._cards.values())
        if cards:
            target = not cards[-1].collapsed
            for c in cards:
                c.collapsed = target
