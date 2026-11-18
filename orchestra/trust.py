"""Trust profiles: which tool calls the face approves on the Observer's behalf, so a
capable model doesn't ask about every `ls`. A profile only ever *adds* approvals to
what Claude Code would already have asked about; it never overrides a deny rule, and
the things below always prompt whatever the profile says: sudo, destructive or
publishing commands, edits to secrets/.git/Claude settings or outside the working
directory, and every MCP tool (they reach outside the machine).

  strict    ask for everything (today's behaviour)
  standard  reads, read-only Bash, edits inside the working directory, planning/agent tools
  trusted   standard + any Bash that is not on the danger list

Trust is a capability question (orchestra/entropy.py, orchestra/capabilities.py): the tier is read from
the session's `coding` capability (trusted >= 60, standard >= 40, strict below), so a weaker model starts
lower and a degrading session (a drop to a weaker model, compaction, a full window) loses trust.
"""

import os
import re
import shlex
from pathlib import Path
from typing import Any

from . import entropy as entropy_mod

PROFILES = ("strict", "standard", "trusted")
READ_TOOLS = {"Read", "Grep", "Glob", "LS", "NotebookRead", "WebFetch", "WebSearch", "ToolSearch"}
EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
PLAIN_TOOLS = {"Agent", "Task", "TodoWrite", "TaskCreate", "TaskUpdate", "TaskList", "TaskGet", "TaskOutput",
               "TaskStop", "SendMessage", "ExitPlanMode"}

SAFE_BASH = {"ls", "pwd", "cat", "head", "tail", "wc", "grep", "rg", "fd", "find", "tree", "stat", "file", "du", "df",
             "echo", "printf", "which", "whoami", "date", "uname", "env", "printenv", "sort", "uniq", "cut", "diff",
             "basename", "dirname", "realpath", "readlink", "id", "hostname", "true", "false", "test", "[", "jq", "nl"}
SAFE_GIT = {"status", "log", "diff", "show", "branch", "remote", "rev-parse", "ls-files", "blame", "describe",
            "shortlog", "tag", "stash", "config", "reflog", "grep"}
# find/sort/tee style flags that make an otherwise-read command write or run something
_UNSAFE_FLAGS = re.compile(r"(^|\s)(-exec|-execdir|-delete|-ok|-fprint\w*|-o\s+\S+|--output(=|\s))", re.I)
_SPLIT = re.compile(r"\|\||&&|;|\||\n|(?<!>)&(?!>)")
_SUBST = re.compile(r"\$\(|`|<\(|>\(")
_REDIRECT_OUT = re.compile(r"(?<![<\d&])>{1,2}(?!\s*(&|/dev/null))")

DANGER = re.compile(
    r"""(^|[\s;&|(])(
        sudo|su|doas|pkexec|
        rm|
        rmdir|shred|truncate|dd|mkfs\S*|fdisk|parted|wipefs|mkswap|
        chmod\s+-R|chown\s+-R|
        git\s+(push|reset\s+--hard|clean|checkout\s+--|restore|branch\s+-D|rebase|filter-branch|gc\s+--prune|update-ref\s+-d)|
        gh\s+(pr|issue|release|repo|api|workflow)\s+\S*(create|merge|close|delete|edit|comment|run|review)|gh\s+api\s+.*(-X|--method)|
        npm\s+(publish|unpublish|deprecate)|pip\s+upload|twine|cargo\s+publish|
        curl\b[^|;&]*\|\s*(ba|z)?sh|wget\b[^|;&]*\|\s*(ba|z)?sh|
        virsh\s+\S*\s*(undefine|destroy)|virsh\b.*--remove-all-storage|
        dnf\s+(install|remove|erase|upgrade|update)|rpm\s+-[ei]|
        systemctl\s+(stop|disable|mask|reboot|poweroff|kill)|reboot|shutdown|poweroff|halt|
        kill|pkill|killall|
        docker\s+(rm|rmi|system\s+prune|volume\s+rm)|podman\s+(rm|rmi|system\s+prune|volume\s+rm)|
        ssh|scp|rsync|nc|ncat|
        esptool\S*|idf\.py\s+\S*(flash|erase)|openocd|
        crontab|mount|umount|iptables|nft|passwd|
        age|sops|gpg\s+--(delete|decrypt)|
        eval|exec|source
    )(\s|$)|\s-(delete|exec|execdir|fprint\w*)(\s|$)""", re.X)
SENSITIVE_PATH = re.compile(r"(^|/)(\.git|\.ssh|\.gnupg|\.aws|\.config/rabble)(/|$)|(^|/)\.env[^/]*$|"
                            r"(^|/)\.claude/(settings[^/]*\.json|hooks)|\.(pem|key)$|id_(rsa|ed25519)|credentials|secrets?\.", re.I)


def profile_for(model: str | None, override: str | None = None, entropy: float | None = None) -> str:
    """The profile in force: an explicit override, else what the session earns (orchestra/entropy.py:
    the tier of its `coding` capability, since trust gates Bash and edits). Given only a score, the
    `general` scale decides; given only a model, its own capability does."""
    if override in PROFILES:
        return override
    if entropy is not None:
        return entropy_mod.trust_for_score(entropy)
    return entropy_mod.Entropy(model).trust


def _segments(command: str) -> list[str]:
    return [s.strip() for s in _SPLIT.split(command) if s.strip()]


def _words(segment: str) -> list[str]:
    try:
        words = shlex.split(segment)
    except ValueError:
        return []
    while words and re.fullmatch(r"[A-Za-z_]\w*=\S*", words[0]):  # leading VAR=value
        words.pop(0)
    return words


def _safe_segment(segment: str) -> bool:
    words = _words(segment)
    if not words or _UNSAFE_FLAGS.search(segment):
        return False
    cmd = os.path.basename(words[0])
    if cmd == "git":
        rest = [w for w in words[1:] if not w.startswith("-")]
        return bool(rest) and rest[0] in SAFE_GIT and not (rest[0] in ("branch", "tag", "stash", "config", "remote") and len(rest) > 1
                                                          and rest[1] not in ("list", "show", "-v"))
    return cmd in SAFE_BASH


def bash_allowed(profile: str, command: str) -> bool:
    if profile == "strict" or not command.strip():
        return False
    if DANGER.search(command) or _REDIRECT_OUT.search(command):
        return False
    if profile == "trusted":
        return True
    if _SUBST.search(command):
        return False
    return all(_safe_segment(s) for s in _segments(command))


def path_allowed(path: str, cwd: str) -> bool:
    try:
        target = Path(path).expanduser()
        target = (target if target.is_absolute() else Path(cwd) / target).resolve()
        return target.is_relative_to(Path(cwd).resolve()) and not SENSITIVE_PATH.search(str(target))
    except (OSError, ValueError, RuntimeError):
        return False


def auto_allow(profile: str, tool: str, tool_input: dict[str, Any], cwd: str) -> bool:
    """True when the profile approves this call without asking."""
    if profile == "strict" or tool.startswith("mcp__"):
        return False
    if tool in READ_TOOLS:
        path = tool_input.get("file_path") or tool_input.get("path") or ""
        return not (path and SENSITIVE_PATH.search(str(path)))
    if tool in PLAIN_TOOLS:
        return True
    if tool in EDIT_TOOLS:
        return path_allowed(tool_input.get("file_path") or tool_input.get("notebook_path") or "", cwd)
    if tool == "Bash":
        return bash_allowed(profile, tool_input.get("command") or "")
    return False
