"""Model picker: Enter returns the chosen value (None = Claude Code's default), Esc
dismisses. The returned object is a 1-tuple so Esc (None) and "default" are distinct."""

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import OptionList, Static
from textual.widgets.option_list import Option

from ui.prefs import MODELS


class ModelPicker(ModalScreen[tuple | None]):
    BINDINGS = [Binding("escape", "close", "close")]

    DEFAULT_CSS = """
    ModelPicker { align: center middle; background: $background 60%; }
    ModelPicker #model-box { width: 50; height: auto; background: $surface; border: round $primary; padding: 1 2; }
    ModelPicker #model-title { color: $primary; text-style: bold; }
    ModelPicker OptionList { height: auto; margin-top: 1; background: $surface; }
    """

    def __init__(self, current: str | None) -> None:
        super().__init__()
        self.current = current

    def compose(self) -> ComposeResult:
        with Vertical(id="model-box"):
            yield Static("Model", id="model-title")
            yield OptionList(*[Option(("● " if v == self.current else "  ") + label + (f"  {v}" if v and v != label else ""), id=label)
                               for label, v in MODELS], id="model-list")

    def on_mount(self) -> None:
        ol = self.query_one(OptionList)
        ol.highlighted = next((i for i, (_, v) in enumerate(MODELS) if v == self.current), 0)
        ol.focus()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        self.dismiss((dict(MODELS)[str(event.option.id)],))

    def action_close(self) -> None:
        self.dismiss(None)
