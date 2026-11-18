#!/usr/bin/env python3
"""rabble: open Claude Code as RaBbLE, with the Crew as agent-team organs.

`rabble --cc` reaches this Claude Code wrapper (S273); bare `rabble` opens RaBbLE-UI
(ui/, S278; see scripts/rabble). Positional words (`rabble "msg"`, `rabble status`) stay
reserved for the L-09 sCoRE streaming CLI.

  rabble --cc                     RaBbLE lead session, Crew available as teammates
  rabble --cc --model opus ...    flags starting with '-' pass straight to claude
  rabble --cc -- "first prompt"   everything after '--' passes to claude
  rabble --cc --crew-list         print the roster and exit
  rabble --cc --crew-print        print the lead prompt + agents JSON and exit

Persona specs are canonical in RaBbLE-Grimoire/RaBbLE/Crew/ (LLM-agnostic); this
file is only the Claude Code adapter. Design: RaBbLE-Grimoire/RaBbLE-sCoRE/RaBbLE-sCoRE-Crew.md

Env: RABBLE_GRIMOIRE (default: sibling of this repo), RABBLE_TEAMMATE_MODE
(default in-process; `tmux`/`auto` panes drop organ tool limits, see below), NO_COLOR.
"""

import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

from roster import (  # noqa: F401  (re-exported for tests and callers)
    ACCESS_TOOLS, CREW_SUBDIR, LEAD_SLUG, PREAMBLE_SLUG, SCORE_ROOT, TIER_MODELS,
    build_agents, build_lead_prompt, grimoire_root, latest_state, load_crew, organs, parse_persona,
)

# claude flags that take a value, so the word after them is not a reserved positional
VALUE_FLAGS = {
    "--model", "--fallback-model", "--effort", "--permission-mode", "--settings", "--add-dir",
    "--mcp-config", "--allowedTools", "--allowed-tools", "--disallowedTools", "--disallowed-tools",
    "--tools", "--session-id", "--resume", "-r", "--agent", "--teammate-mode", "--name", "-n",
    "--setting-sources", "--plugin-dir", "--append-system-prompt", "--system-prompt",
}
HEADLESS_FLAGS = {"-p", "--print"}
PROMPT_KEEP_SECONDS = 7 * 24 * 3600
RESERVED_MSG = (
    "rabble: positional words are reserved for the sCoRE streaming CLI (L-09, not built yet).\n"
    "  rabble                     open RaBbLE-UI\n"
    "  rabble --cc                open the RaBbLE Claude Code session\n"
    "  rabble --cc -- \"prompt\"   open it with a first prompt\n"
)
# Crew Thread (candidate, RaBbLE-Palette-Candidates.md); muted text is canonical
THREAD = "#31c7d5"
MUTED = "#6b6880"


# ── banner ──────────────────────────────────────────────────────────────────

def _rgb(hexcolor):
    h = hexcolor.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def _paint(text, hexcolor):
    r, g, b = _rgb(hexcolor)
    return f"\033[38;2;{r};{g};{b}m{text}\033[0m"


def _gradient(text, a, b):
    ra, rb = _rgb(a), _rgb(b)
    n = max(len(text) - 1, 1)
    out = []
    for i, ch in enumerate(text):
        t = i / n
        r, g, bl = (round(x + (y - x) * t) for x, y in zip(ra, rb))
        out.append(f"\033[1;38;2;{r};{g};{bl}m{ch}")
    return "".join(out) + "\033[0m"


def banner(personas, color=True):
    lead = personas[LEAD_SLUG][0]
    names = []
    for slug, m, _ in organs(personas):
        # The Archivist's colours are the void itself; show it in canonical muted text.
        hexcolor = MUTED if slug == "archivist" else m.get("color_base", MUTED)
        names.append(_paint(m["name"], hexcolor) if color else m["name"])
    sep = _paint(" · ", THREAD) if color else " · "
    title = _gradient("RaBbLE", lead.get("color_base", "#ff2d78"), lead.get("color_variant", "#00f5ff")) if color else "RaBbLE"
    crew = _paint("the Crew", THREAD) if color else "the Crew"
    return f"{title} {crew}  {sep.join(names)}\n"


# ── entry ───────────────────────────────────────────────────────────────────

def split_args(argv):
    """Returns (own_flag, claude_args). Raises SystemExit on reserved positional use."""
    own = None
    rest = []
    passthrough = False
    prev = None
    for arg in argv:
        if passthrough:
            rest.append(arg)
        elif arg == "--":
            passthrough = True
        elif arg in ("--crew-list", "--crew-print"):
            own = arg
        elif arg in HEADLESS_FLAGS:
            sys.stderr.write("rabble: the Crew needs an interactive session; use plain `claude -p` for headless runs\n")
            raise SystemExit(2)
        elif not arg.startswith("-") and prev not in VALUE_FLAGS:
            sys.stderr.write(RESERVED_MSG)
            raise SystemExit(2)
        else:
            rest.append(arg)
        prev = arg
    return own, rest


def claude_argv(prompt_file, agents, args):
    argv = ["claude", "--append-system-prompt-file", str(prompt_file), "--agents", json.dumps(agents)]
    if not any(a == "--teammate-mode" or a.startswith("--teammate-mode=") for a in args):
        argv += ["--teammate-mode", teammate_mode()]
    return argv + args


def teammate_mode():
    return os.environ.get("RABBLE_TEAMMATE_MODE", "in-process")


def write_prompt_file(cache, text):
    """One file per launch (concurrent launches never truncate each other); prune old ones."""
    cache.mkdir(parents=True, exist_ok=True)
    cutoff = time.time() - PROMPT_KEEP_SECONDS
    for old in cache.glob("crew-lead-*.md"):
        try:
            if old.stat().st_mtime < cutoff:
                old.unlink()
        except OSError:
            pass
    fd, name = tempfile.mkstemp(prefix="crew-lead-", suffix=".md", dir=cache)  # 0600
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    return Path(name)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    own, args = split_args(argv)
    grimoire = grimoire_root()
    crew_dir = grimoire / CREW_SUBDIR
    try:
        personas = load_crew(crew_dir)
        agents = build_agents(personas)
    except (OSError, ValueError) as e:
        sys.stderr.write(f"rabble: cannot read the Crew from {crew_dir}: {e}\n")
        return 1
    color = sys.stderr.isatty() and "NO_COLOR" not in os.environ
    lead_prompt = build_lead_prompt(personas, grimoire)

    if own == "--crew-list":
        sys.stdout.write(banner(personas, color=sys.stdout.isatty() and "NO_COLOR" not in os.environ))
        return 0
    if own == "--crew-print":
        sys.stdout.write(lead_prompt + "\n---- agents ----\n" + json.dumps(agents, indent=2) + "\n")
        return 0

    if not shutil.which("claude"):
        sys.stderr.write("rabble: `claude` not found on PATH (layerctl apply claude-code)\n")
        return 1
    cache = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "rabble"
    prompt_file = write_prompt_file(cache, lead_prompt)

    sys.stderr.write(banner(personas, color))
    if Path.cwd().resolve().is_relative_to(SCORE_ROOT):
        # sCoRE's .claude/settings.json denies Bash and Agent for every session started in it
        sys.stderr.write("rabble: inside RaBbLE-sCoRE, whose settings block Bash and teammates; "
                         "start from the Collective root instead\n")
    explicit = next((a.split("=", 1)[1] if "=" in a else None for a in args if a.startswith("--teammate-mode")), None)
    if (explicit or teammate_mode()) != "in-process":
        # split-pane teammates replace Claude Code's default prompt with the organ body
        # and ignore disallowedTools; the `tools` allowlist still holds
        sys.stderr.write("rabble: pane mode: organs run without Claude Code's default prompt; "
                         "in-process is the tested mode\n")
    # inherited by claude and its children (e.g. a nested claude run started by an organ)
    os.environ["CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS"] = "1"
    os.execvp("claude", claude_argv(prompt_file, agents, args))


if __name__ == "__main__":
    sys.exit(main())
