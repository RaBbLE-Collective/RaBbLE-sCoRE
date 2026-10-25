"""Resume picker: earlier sessions for this project, newest first.

Enter returns the session id, Esc returns None. Typing filters the list.
"""

import time

from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, OptionList, Static
from textual.widgets.option_list import Option

from orchestra.sessions import SessionRow, recent_sessions


def ago(ts: float, now: float | None = None) -> str:
    d = max(0, int((now or time.time()) - ts))
    for span, unit in ((86400, "d"), (3600, "h"), (60, "m")):
        if d >= span:
            return f"{d // span}{unit} ago"
    return "just now"


class SessionPicker(ModalScreen[str | None]):
    BINDINGS = [
        Binding("escape", "close", "close"),
        Binding("down", "move(1)", show=False),
        Binding("up", "move(-1)", show=False),
    ]

    DEFAULT_CSS = """
    SessionPicker { align: center middle; background: $background 60%; }
    SessionPicker #sess-box {
        width: 90%; max-width: 100; height: auto; max-height: 80%;
        background: $surface; border: round $primary; padding: 1 2;
    }
    SessionPicker #sess-title { color: $primary; text-style: bold; }
    SessionPicker #sess-empty { color: $foreground 70%; margin-top: 1; }
    SessionPicker OptionList { height: auto; max-height: 20; margin-top: 1; background: $surface; }
    """

    def __init__(self, cwd: str) -> None:
        super().__init__()
        self.cwd = cwd
        self.rows: list[SessionRow] = []

    def compose(self) -> ComposeResult:
        with Vertical(id="sess-box"):
            yield Static("Resume a session", id="sess-title")
            yield Input(placeholder="filter", id="sess-filter")
            yield Static("Loading…", id="sess-empty", markup=False)
            yield OptionList(id="sess-list")

    def on_mount(self) -> None:
        self.query_one("#sess-list").display = False
        self._load()

    @work(thread=True)
    def _load(self) -> None:
        rows = recent_sessions(self.cwd)
        self.app.call_from_thread(self._loaded, rows)

    def _loaded(self, rows: list[SessionRow]) -> None:
        self.rows = rows
        self._fill("")

    def _fill(self, needle: str) -> None:
        ol = self.query_one("#sess-list", OptionList)
        empty = self.query_one("#sess-empty", Static)
        needle = needle.lower().strip()
        shown = [r for r in self.rows if needle in r.title.lower()]
        ol.clear_options()
        if not shown:
            empty.update("No earlier sessions here" if not self.rows else "No match")
            empty.display = True
            ol.display = False
            return
        empty.display = False
        ol.display = True
        for r in shown:
            turns = f" · {r.turns} turns" if r.turns is not None else ""
            ol.add_option(Option(f"{ago(r.updated)} · {r.title}{turns}", id=r.id))
        ol.highlighted = 0

    def on_input_changed(self, event: Input.Changed) -> None:
        self._fill(event.value)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        ol = self.query_one("#sess-list", OptionList)
        if ol.display and ol.highlighted is not None:
            self.dismiss(ol.get_option_at_index(ol.highlighted).id)

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(event.option.id)

    def action_move(self, step: int) -> None:
        ol = self.query_one("#sess-list", OptionList)
        ol.action_cursor_down() if step > 0 else ol.action_cursor_up()

    def action_close(self) -> None:
        self.dismiss(None)
