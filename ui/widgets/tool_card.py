"""One tool call as a collapsible card: name + one-line input, result on the title."""

import json

from rich.text import Text
from textual.widgets import Collapsible, Static

TITLE_MAX = 100


def one_line(name: str, data: dict) -> str:
    for key in ("command", "file_path", "path", "pattern", "description", "url", "query"):
        if data.get(key):
            return " ".join(str(data[key]).split())
    return " ".join(json.dumps(data, ensure_ascii=False).split()) if data else ""


def _clip(text: str, n: int = TITLE_MAX) -> str:
    return text if len(text) <= n else text[: n - 1] + "…"


class ToolCard(Collapsible):
    DEFAULT_CSS = """
    ToolCard { border: none; padding: 0 0 0 1; margin: 0; background: transparent; }
    ToolCard > Contents { padding: 0 0 0 2; }
    ToolCard.-error CollapsibleTitle { color: $error; }
    ToolCard.-ok CollapsibleTitle { color: $text-muted; }
    """

    def __init__(self, tool_id: str, organ_label: str, name: str, data: dict):
        self.tool_id, self.tool_name = tool_id, name
        self.line = _clip(one_line(name, data))
        self.prefix = f"{organ_label}: " if organ_label else ""
        self.result = ""
        self._body = json.dumps(data, ensure_ascii=False, indent=2)
        self._full = Static(Text(self._body))
        super().__init__(self._full, title=self._label(), collapsed=True)

    def _label(self) -> str:
        done = f" → {_clip(self.result, 60)}" if self.result else ""
        # the title is parsed as Textual markup, and neither rich's nor textual's escape() catches every
        # bracket run (a printed list of Regions raised MarkupError and killed the pump): escape each "["
        return f"{self.prefix}{self.tool_name}  {self.line}{done}".replace("[", "\\[")

    def set_result(self, ok: bool, summary: str) -> None:
        self.result = summary or ("ok" if ok else "failed")
        self.add_class("-ok" if ok else "-error")
        self.title = self._label()
        if summary:
            self._full.update(Text(self._body + "\n\n" + summary))
