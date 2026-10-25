"""Composer: multiline input. Enter sends, Shift+Enter (or Ctrl+J) newline, Up/Down
recall history at the first/last line, `@path` completion (Tab accepts), Esc
interrupts, Shift+Tab cycles permission mode, Ctrl+C/Ctrl+D feed the app's quit rules.

Keys are handled here (not as app bindings) because TextArea and Screen both bind
several of them and the focused widget would otherwise lose to them.
"""

import os
import re
from pathlib import Path

from textual.containers import Vertical
from textual.message import Message
from textual.widgets import OptionList, TextArea
from textual.widgets.option_list import Option

SKIP_DIRS = {".git", "node_modules", ".venv", "__pycache__", ".cache", "dist", "build"}
SCAN_CAP = 5000
MAX_DEPTH = 6
SHOW = 8
_AT = re.compile(r"(?:^|\s)@([^\s@]*)$")


def scan_files(root: str, cap: int = SCAN_CAP) -> list[str]:
    out: list[str] = []
    base = Path(root)
    for dirpath, dirs, files in os.walk(base):
        rel = Path(dirpath).relative_to(base)
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not (d.startswith(".") and d != ".claude"))
        if len(rel.parts) >= MAX_DEPTH:
            dirs[:] = []
        for d in dirs:
            out.append(str(rel / d) + "/" if rel.parts else d + "/")
        for f in sorted(files):
            out.append(str(rel / f) if rel.parts else f)
        if len(out) >= cap:
            break
    return out[:cap]


def match(files: list[str], frag: str, limit: int = SHOW) -> list[str]:
    frag = frag.lower()
    hits = [f for f in files if frag in f.lower()]
    hits.sort(key=lambda f: (not f.lower().startswith(frag), not os.path.basename(f.rstrip("/")).lower().startswith(frag), len(f)))
    return hits[:limit]


class Composer(TextArea):
    class Submit(Message):
        def __init__(self, text: str):
            super().__init__()
            self.text = text

    class Command(Message):
        """interrupt | cycle_mode | ctrl_c | ctrl_d"""
        def __init__(self, name: str):
            super().__init__()
            self.name = name

    def __init__(self, cwd: str, dropdown: OptionList, **kw):
        super().__init__(show_line_numbers=False, soft_wrap=True, **kw)
        self.cwd = cwd
        self._dropdown = dropdown
        self._files: list[str] | None = None
        self._frag_len = 0
        self._history: list[str] = []
        self._pos: int | None = None
        self._draft = ""

    # completion
    @property
    def completing(self) -> bool:
        return self._dropdown.display

    def _hide(self) -> None:
        self._dropdown.display = False

    def on_text_area_changed(self, event) -> None:
        row, col = self.cursor_location
        before = self.document[row][:col]
        m = _AT.search(before)
        if not m:
            self._hide()
            return
        if self._files is None:
            self._files = scan_files(self.cwd)
        hits = match(self._files, m.group(1))
        self._frag_len = len(m.group(1))
        self._dropdown.clear_options()
        if not hits:
            self._hide()
            return
        self._dropdown.add_options([Option(h, id=h) for h in hits])
        self._dropdown.highlighted = 0
        self._dropdown.display = True

    def _accept(self) -> None:
        idx = self._dropdown.highlighted or 0
        if idx >= self._dropdown.option_count:
            return
        path = str(self._dropdown.get_option_at_index(idx).id)
        row, col = self.cursor_location
        self.replace(path if path.endswith("/") else path + " ", (row, col - self._frag_len), (row, col))
        self._hide()

    # history
    def _recall(self, step: int) -> None:
        if not self._history:
            return
        if self._pos is None:
            self._draft = self.text
            self._pos = len(self._history)
        self._pos = max(0, min(len(self._history), self._pos + step))
        self.load_text(self._draft if self._pos == len(self._history) else self._history[self._pos])
        self.move_cursor(self.document.end)

    def remember(self, text: str) -> None:
        if text and (not self._history or self._history[-1] != text):
            self._history.append(text)
        self._pos = None

    async def _on_key(self, event) -> None:
        key = event.key
        handled = True
        if key == "enter":
            text = self.text.strip()
            self._hide()
            if text:
                self.remember(text)
                self.load_text("")
                self.post_message(self.Submit(text))
        elif key in ("shift+enter", "ctrl+j"):
            self.insert("\n")
        elif key == "tab" and self.completing:
            self._accept()
        elif key == "escape":
            if self.completing:
                self._hide()
            else:
                self.post_message(self.Command("interrupt"))
        elif key == "shift+tab":
            self.post_message(self.Command("cycle_mode"))
        elif key == "ctrl+c" and not self.selected_text:
            self.post_message(self.Command("ctrl_c"))
        elif key == "ctrl+d" and not self.text:
            self.post_message(self.Command("ctrl_d"))
        elif key in ("up", "down") and self.completing:
            self._dropdown.action_cursor_up() if key == "up" else self._dropdown.action_cursor_down()
        elif key == "up" and self.cursor_at_first_line and not self.selected_text:
            self._recall(-1)
        elif key == "down" and self.cursor_at_last_line and self._pos is not None:
            self._recall(1)
        else:
            handled = False
        if handled:
            event.stop()
            event.prevent_default()


class Prompt(Vertical):
    """Dropdown above the input; holds both so the app mounts one thing."""
    DEFAULT_CSS = """
    Prompt { height: auto; dock: bottom; }
    Prompt OptionList { height: auto; max-height: 9; display: none; border: none; background: $surface; }
    Prompt Composer { height: auto; min-height: 3; max-height: 10; border: round $primary; background: $surface; }
    Prompt Composer:focus { border: round $accent; }
    """

    def __init__(self, cwd: str):
        super().__init__()
        self.dropdown = OptionList()
        self.composer = Composer(cwd, self.dropdown)

    def compose(self):
        yield self.dropdown
        yield self.composer
