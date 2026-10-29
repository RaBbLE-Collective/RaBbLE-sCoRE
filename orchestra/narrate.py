"""Plain-English one-sentence narration of what the model is doing.

Pure functions: a tool name and its input in, a sentence out. The face shows these
beside the real transcript so a glance says what is happening without reading tool
JSON. Arguments are never echoed raw: a path is shown by its last two parts, a shell
command only by what kind of work it is, anything long is truncated, and anything that
looks like a credential is replaced. Unknown tools fall back to "Using <tool>".
"""

import os
import re
from typing import Any, Callable

MAX_ARG = 60
MAX_SENTENCE = 120
WAITING = "Waiting for your answer on a permission"

_SECRET = re.compile(
    r"(?i)(sk-[\w-]{8,}|gh[pousr]_\w{8,}|xox[abp]-[\w-]{8,}|AKIA[0-9A-Z]{12,}|eyJ[\w-]{10,}\.[\w-]{5,}"
    r"|(?:password|passwd|secret|token|api[_-]?key|authorization|bearer)\b\s*[=:]?\s*\S+)")
_TESTS = re.compile(r"\b(pytest|cargo\s+test|npm\s+(?:run\s+)?test|go\s+test|unittest|vitest|jest|ctest|make\s+test)\b")


def clip(text: Any, limit: int = MAX_ARG) -> str:
    """One line, secrets masked, truncated."""
    s = " ".join(str(text or "").split())
    s = _SECRET.sub("[hidden]", s)
    return s if len(s) <= limit else s[:limit - 1] + "…"


def _path(value: Any) -> str:
    p = str(value or "").strip()
    if not p:
        return ""
    parts = [x for x in p.replace("\\", "/").split("/") if x]
    return clip("/".join(parts[-2:]) if parts else p)


def _tick(text: str) -> str:
    return f"`{text}`" if text else ""


def _with(verb: str, arg: str, bare: str) -> str:
    return f"{verb} {_tick(arg)}" if arg else bare


def _bash(i: dict) -> str:
    cmd = str(i.get("command") or "")
    if _SECRET.search(cmd):
        return "Running a shell command"
    if _TESTS.search(cmd):
        return "Running the tests"
    first = cmd.strip().split(None, 1)[0] if cmd.strip() else ""
    word = os.path.basename(first)
    kinds = {"git": "Running a git command", "ls": "Listing files", "grep": "Searching the code", "rg": "Searching the code",
             "find": "Searching for files", "cargo": "Running cargo", "npm": "Running npm", "pip": "Running pip",
             "python": "Running a Python script", "python3": "Running a Python script", "make": "Running make",
             "docker": "Running docker", "systemctl": "Talking to systemd", "curl": "Fetching a URL"}
    return kinds.get(word, "Running a shell command")


def _agent(i: dict) -> str:
    organ = clip(i.get("subagent_type") or "agent", 30)
    task = clip(i.get("description") or i.get("prompt") or "", 70)
    return f"Asking the {organ} organ to {task[0].lower() + task[1:]}" if task else f"Asking the {organ} organ for help"


_TOOLS: dict[str, Callable[[dict], str]] = {
    "Read": lambda i: _with("Reading", _path(i.get("file_path")), "Reading a file"),
    "Edit": lambda i: _with("Editing", _path(i.get("file_path")), "Editing a file"),
    "MultiEdit": lambda i: _with("Editing", _path(i.get("file_path")), "Editing a file"),
    "Write": lambda i: _with("Writing", _path(i.get("file_path")), "Writing a file"),
    "NotebookEdit": lambda i: _with("Editing notebook", _path(i.get("notebook_path")), "Editing a notebook"),
    "Glob": lambda i: _with("Looking for files matching", clip(i.get("pattern")), "Looking for files"),
    "Grep": lambda i: _with("Searching the code for", clip(i.get("pattern")), "Searching the code"),
    "Bash": _bash,
    "BashOutput": lambda i: "Checking on a background command",
    "KillShell": lambda i: "Stopping a background command",
    "WebFetch": lambda i: "Fetching a web page",
    "WebSearch": lambda i: _with("Searching the web for", clip(i.get("query")), "Searching the web"),
    "TodoWrite": lambda i: "Updating the task list",
    "Agent": _agent,
    "Task": _agent,
    "ExitPlanMode": lambda i: "Presenting a plan for your approval",
    "AskUserQuestion": lambda i: "Asking you a question",
    "Skill": lambda i: _with("Using the skill", clip(i.get("skill") or i.get("name"), 40), "Using a skill"),
}


def tool(name: str, tool_input: dict[str, Any] | None) -> str:
    """One sentence for a tool call. Never raises: bad input degrades to the fallback."""
    i = tool_input if isinstance(tool_input, dict) else {}
    fn = _TOOLS.get(name)
    if fn is None and name.startswith("mcp__"):
        parts = name.split("__")
        return f"Using {clip(parts[-1].replace('_', ' '), 40)} from {clip(parts[1], 30)}" if len(parts) >= 3 else f"Using {clip(name, 40)}"
    try:
        sentence = fn(i) if fn else f"Using {clip(name, 40) or 'a tool'}"
    except Exception:
        sentence = f"Using {clip(name, 40) or 'a tool'}"
    return clip(sentence, MAX_SENTENCE)


def permission(tool_name: str = "") -> str:
    return WAITING
