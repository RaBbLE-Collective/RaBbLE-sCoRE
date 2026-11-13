"""Modal for a PermissionAsk: show exactly what will run, take the Observer's answer.

Keyboard-first (y/1 allow, a/2 allow for session, n/3/Esc deny + optional reason) so
it works over SSH and tmux. Bodies are plain Static text: selectable, no markup.
Colours come from theme variables only.
"""

import difflib
import json
import time

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Static

from orchestra.events import LEAD, PermissionAsk, PermissionReply

VALUE_MAX = 2048
WRITE_LINES = 40


def _clip(text: str, n: int = VALUE_MAX) -> str:
    return text if len(text) <= n else text[:n] + f" … (+{len(text) - n} chars)"


def preview(tool: str, data: dict) -> str:
    """Plain-text preview of what the tool will do. Bash is never truncated."""
    if tool == "Bash":
        return str(data.get("command", ""))
    if tool == "Edit":
        old = str(data.get("old_string", "")).splitlines()
        new = str(data.get("new_string", "")).splitlines()
        path = data.get("file_path", "")
        diff = difflib.unified_diff(old, new, f"a/{path}", f"b/{path}", lineterm="")
        return f"{path}\n" + "\n".join(diff)
    if tool == "Write":
        lines = str(data.get("content", "")).splitlines()
        body = "\n".join(lines[:WRITE_LINES])
        more = len(lines) - WRITE_LINES
        tail = f"\n… (+{more} more lines)" if more > 0 else ""
        return f"{data.get('file_path', '')}\n{body}{tail}"
    if tool in ("MultiEdit", "NotebookEdit"):
        path = data.get("file_path") or data.get("notebook_path", "")
        n = len(data.get("edits", [])) if tool == "MultiEdit" else 1
        return f"{path}\n{n} edit{'s' if n != 1 else ''}"
    short = {k: (_clip(v) if isinstance(v, str) else v) for k, v in data.items()}
    return _clip(json.dumps(short, indent=2, ensure_ascii=False, default=str), VALUE_MAX * 2)


class PermissionScreen(ModalScreen[PermissionReply]):
    BINDINGS = [
        Binding("y,1", "choose('allow')", "allow"),
        Binding("a,2", "choose('allow_session')", "allow session"),
        Binding("n,3", "deny", "deny"),
        Binding("escape", "deny_now", "deny", show=False),
    ]

    DEFAULT_CSS = """
    PermissionScreen { align: center middle; background: $background 60%; }
    PermissionScreen #perm-box {
        width: 90%; max-width: 110; height: auto; max-height: 90%;
        background: $surface; border: round $warning; padding: 1 2;
    }
    PermissionScreen #perm-who { color: $warning; text-style: bold; }
    PermissionScreen .perm-meta { color: $foreground 70%; margin-top: 1; }
    PermissionScreen #perm-path { color: $accent; }
    PermissionScreen #perm-body-wrap {
        height: auto; max-height: 20; margin-top: 1;
        background: $panel; padding: 0 1;
    }
    PermissionScreen #perm-body { width: 100%; }
    PermissionScreen #perm-buttons { height: auto; margin-top: 1; }
    PermissionScreen #perm-buttons Button { margin-right: 2; }
    PermissionScreen #perm-allow { background: $primary; color: $background; }
    PermissionScreen #perm-session { background: $secondary; color: $background; }
    PermissionScreen #perm-deny { background: $error; color: $background; }
    PermissionScreen #perm-reason { margin-top: 1; display: none; }
    PermissionScreen.-denying #perm-reason { display: block; }
    PermissionScreen.-denying #perm-buttons { display: none; }
    /* short pane: drop the chrome so the buttons always fit; the body scroll gives up its rows first */
    PermissionScreen.-short #perm-box { max-height: 100%; padding: 0 1; }
    PermissionScreen.-short .perm-meta, PermissionScreen.-short #perm-body-wrap,
    PermissionScreen.-short #perm-buttons, PermissionScreen.-short #perm-reason { margin-top: 0; }
    PermissionScreen.-short #perm-buttons Button { margin-right: 1; }
    PermissionScreen.-short #perm-reason { height: 1; border: none; padding: 0; }
    PermissionScreen.-tiny .perm-meta { display: none; }
    """

    SHORT_ROWS = 26
    TINY_ROWS = 12
    BODY_MAX = 20
    BODY_MIN = 3

    # keys are ignored this long after the modal appears: a keystroke meant for the composer
    # (or a second "y" meant for the previous ask) must not answer an ask nobody has read yet.
    # Esc (deny) is never held back.
    ARM_SECONDS = 0.4

    def __init__(self, ask: PermissionAsk) -> None:
        super().__init__()
        self.ask = ask
        self._armed_at = float("inf")

    def on_mount(self) -> None:
        self._armed_at = time.monotonic() + self.ARM_SECONDS
        self._fit()

    def on_resize(self) -> None:
        self._fit()

    def _fit(self) -> None:
        """Keep the answer controls on screen: compact below SHORT_ROWS, and cap the body
        scroll to whatever rows the rest of the box leaves free."""
        rows = self.size.height
        if not rows:
            return
        self.set_class(rows < self.SHORT_ROWS, "-short")
        self.set_class(rows < self.TINY_ROWS, "-tiny")
        for b in self.query("#perm-buttons Button"):
            b.compact = rows < self.SHORT_ROWS
        self.call_after_refresh(self._fit_body)

    def _fit_body(self) -> None:
        box = self.query_one("#perm-box")
        wrap = self.query_one("#perm-body-wrap")
        others = sum(
            w.outer_size.height + w.styles.margin.top + w.styles.margin.bottom
            for w in box.children
            if w is not wrap and w.display
        )
        free = self.size.height - box.gutter.height - others - wrap.styles.margin.top - wrap.styles.margin.bottom
        wrap.styles.max_height = max(self.BODY_MIN, min(self.BODY_MAX, free))

    def _armed(self) -> bool:
        return time.monotonic() >= self._armed_at

    def compose(self) -> ComposeResult:
        a = self.ask
        who = "RaBbLE" if a.organ == LEAD else a.organ
        with Vertical(id="perm-box"):
            yield Static(f"{who} asks to use {a.display_name or a.tool}", id="perm-who", markup=False)
            if a.title:
                yield Static(a.title, classes="perm-meta", markup=False)
            if a.description:
                yield Static(a.description, classes="perm-meta", markup=False)
            if a.blocked_path:
                yield Static(f"path: {a.blocked_path}", id="perm-path", markup=False)
            with VerticalScroll(id="perm-body-wrap"):
                yield Static(preview(a.tool, a.input), id="perm-body", markup=False)
            with Horizontal(id="perm-buttons"):
                yield Button("1 allow once", id="perm-allow")
                yield Button("2 allow for session", id="perm-session")
                yield Button("3 deny", id="perm-deny")
            yield Input(placeholder="why not? (optional, Enter to deny)", id="perm-reason")

    def action_choose(self, decision: str) -> None:
        if self.has_class("-denying") or not self._armed():
            return
        self.dismiss(PermissionReply(decision))  # type: ignore[arg-type]

    def action_deny(self) -> None:
        if not self._armed():
            return
        self.add_class("-denying")
        self.query_one("#perm-reason", Input).focus()
        self.call_after_refresh(self._fit_body)

    def action_deny_now(self) -> None:
        self.dismiss(PermissionReply("deny"))

    @on(Button.Pressed, "#perm-allow")
    def _allow(self) -> None:
        self.action_choose("allow")

    @on(Button.Pressed, "#perm-session")
    def _session(self) -> None:
        self.action_choose("allow_session")

    @on(Button.Pressed, "#perm-deny")
    def _deny(self) -> None:
        self.action_deny()

    @on(Input.Submitted, "#perm-reason")
    def _reason(self, event: Input.Submitted) -> None:
        self.dismiss(PermissionReply("deny", event.value.strip()))
