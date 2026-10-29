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
    r"(?i)(?:sk-[\w-]{8,}|sk_(?:live|test)_\w{8,}|gsk_\w{8,}|gh[pousr]_\w{8,}|github_pat_\w{8,}|glpat-[\w-]{8,}|hf_\w{8,}"
    r"|xox[abp]-[\w-]{8,}|AKIA[0-9A-Z]{12,}|AIza[\w-]{20,}|npm_\w{16,}|eyJ[\w-]{10,}\.[\w-]{5,}"
    r"|(?:password|passwd|secret|token|api[_-]?key|authorization)\s*[=:]\s*\S{6,}|bearer\s+\S{8,}"
    r"|[0-9a-f]{40,})")
_LONG_RUN = re.compile(r"[A-Za-z0-9_-]{32,}")   # long high-entropy runs: letters and digits mixed
_CONTROL = re.compile("[\x00-\x1f\x7f-\x9f\u200e\u200f\u202a-\u202e\u2066-\u2069]")
_OPS = re.compile(r"[;|]|&&")
_TESTS = re.compile(r"\b(pytest|cargo\s+test|npm\s+(?:run\s+)?test|go\s+test|unittest|vitest|jest|ctest|make\s+test)\b")


def clip(text: Any, limit: int = MAX_ARG) -> str:
    """One line, secrets masked, truncated."""
    s = " ".join(_CONTROL.sub(" ", str(text or "")).split())
    s = _SECRET.sub("[hidden]", s)
    s = _LONG_RUN.sub(lambda m: "[hidden]" if re.search(r"\d", m[0]) and re.search(r"[A-Za-z]", m[0]) else m[0], s)
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
    cmd = str(i.get("command") or "").strip()
    if _SECRET.search(cmd) or _LONG_RUN.search(cmd):
        return "Running a shell command"
    chained = bool(_OPS.search(cmd))
    if _TESTS.search(cmd) and not chained:
        return "Running the tests"
    word = os.path.basename(cmd.split(None, 1)[0]) if cmd else ""
    if word in ("pytest", "py.test") and _TESTS.search(word):
        return "Running the tests"
    kinds = {"git": "Running a git command", "ls": "Listing files", "grep": "Searching the code", "rg": "Searching the code",
             "find": "Searching for files", "cargo": "Running cargo", "npm": "Running npm", "pip": "Running pip",
             "python": "Running a Python script", "python3": "Running a Python script", "make": "Running make",
             "docker": "Running docker", "systemctl": "Talking to systemd", "curl": "Fetching a URL"}
    return kinds.get(word, "Running a shell command")


def _lower(task: str) -> str:
    return task[0].lower() + task[1:] if len(task) > 1 and task[1].islower() else task


def _agent(i: dict) -> str:
    organ = clip(i.get("subagent_type") or "agent", 30)
    task = clip(i.get("description") or i.get("prompt") or "", 70)
    return f"Asking the {organ} organ to {_lower(task)}" if task else f"Asking the {organ} organ for help"


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
    name = str(name or "")
    i = tool_input if isinstance(tool_input, dict) else {}
    try:
        fn = _TOOLS.get(name)
        if fn is None and name.startswith("mcp__"):
            parts = [x for x in name.split("__") if x]
            if len(parts) >= 3:
                sentence = f"Using {clip(parts[-1].replace('_', ' '), 40)} from {clip(parts[1], 30)}"
            else:
                sentence = f"Using {clip(parts[-1] if len(parts) > 1 else name, 40)}"
        else:
            sentence = fn(i) if fn else f"Using {clip(name, 40) or 'a tool'}"
    except Exception:
        sentence = f"Using {clip(name, 40) or 'a tool'}"
    return clip(sentence, MAX_SENTENCE)


def permission(tool_name: str = "") -> str:
    return WAITING
