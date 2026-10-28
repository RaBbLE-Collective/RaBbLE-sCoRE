"""The Crew roster: persona specs from RaBbLE-Grimoire/RaBbLE/Crew/ turned into
organ definitions and the lead prompt. Shared by the Claude Code wrapper (rabble.py)
and RaBbLE-UI (orchestra/). No colours here: those come from the palette and persona
frontmatter at the point of drawing.

Env: RABBLE_GRIMOIRE (default: sibling of this repo).
"""

import os
import re
from pathlib import Path

SCORE_ROOT = Path(__file__).resolve().parent.parent
CREW_SUBDIR = Path("RaBbLE") / "Crew"
LEAD_SLUG = "rabble"
PREAMBLE_SLUG = "_organ"
TIER_MODELS = {"haiku": "haiku", "sonnet": "sonnet", "opus": "opus"}
# access -> (tools allowlist, tools denied). The allowlist (`tools`) holds in every display
# mode; `disallowedTools` only for in-process teammates. Bash stays for inspection (git log,
# test runs), so read-only means "no file-edit tools", not "no side effects".
# `scoped` is instruction-level only: teammates don't apply per-agent hooks.
_NO_EDIT = ["Edit", "Write", "NotebookEdit"]
ACCESS_TOOLS = {
    "read-only": (["Read", "Grep", "Glob", "Bash", "WebFetch", "WebSearch"], _NO_EDIT),
    "run": (["Read", "Grep", "Glob", "Bash"], _NO_EDIT),
    "full": (None, []),
    "scoped": (None, []),
}


def grimoire_root():
    env = os.environ.get("RABBLE_GRIMOIRE")
    return Path(env).expanduser() if env else SCORE_ROOT.parent / "RaBbLE-Grimoire"


def parse_persona(path):
    """Flat `key: value` frontmatter between --- lines, then the body."""
    text = path.read_text(encoding="utf-8-sig").replace("\r\n", "\n")
    m = re.match(r"^---\n(.*?)\n---\n?(.*)$", text, re.S)
    if not m:
        raise ValueError(f"{path}: missing frontmatter")
    meta = {}
    for line in m.group(1).splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, sep, value = line.partition(":")
        if not sep:
            raise ValueError(f"{path}: bad frontmatter line: {line!r}")
        meta[key.strip()] = value.strip()
    for required in ("name", "slug"):
        if required not in meta:
            raise ValueError(f"{path}: frontmatter needs '{required}'")
    return meta, m.group(2).strip()


def load_crew(crew_dir):
    personas = {}
    for path in sorted(crew_dir.glob("*.md")):
        if path.name == "README.md":
            continue
        meta, body = parse_persona(path)
        if meta["slug"] in personas:
            raise ValueError(f"{path}: duplicate slug '{meta['slug']}'")
        personas[meta["slug"]] = (meta, body)
    for slug in (LEAD_SLUG, PREAMBLE_SLUG):
        if slug not in personas:
            raise ValueError(f"{crew_dir}: missing {slug}.md")
    return personas


def organs(personas):
    found = [(s, m, b) for s, (m, b) in personas.items() if s not in (LEAD_SLUG, PREAMBLE_SLUG)]
    return sorted(found, key=lambda o: (int(o[1].get("order", 99)), o[0]))


def build_agents(personas):
    """Claude Code --agents JSON: one agent type per organ."""
    preamble = personas[PREAMBLE_SLUG][1]
    agents = {}
    for slug, meta, body in organs(personas):
        access = meta.get("access", "read-only")
        if access not in ACCESS_TOOLS:
            raise ValueError(f"{slug}: unknown access '{access}'")
        agent = {
            "description": f"{meta['name']}: {meta.get('role', '')}. {meta.get('call_when', '')}".strip(),
            "prompt": f"{preamble}\n\n{body}",
        }
        model = TIER_MODELS.get(meta.get("tier", ""))
        if model:
            agent["model"] = model
        allow, deny = ACCESS_TOOLS[access]
        if allow:
            agent["tools"] = allow
        if deny:
            agent["disallowedTools"] = deny
        agents[slug] = agent
    return agents


def latest_state(grimoire):
    """The SESSION-LOG ## LATEST box, so RaBbLE starts aware of where things stand."""
    log = grimoire / "log" / "SESSION-LOG.md"
    try:
        text = log.read_text(encoding="utf-8")
    except OSError:
        return ""
    m = re.search(r"^## LATEST.*?(?=^---)", text, re.S | re.M)
    return m.group(0).strip() if m else ""


def build_lead_prompt(personas, grimoire):
    meta, body = personas[LEAD_SLUG]
    rows = []
    for slug, m, _ in organs(personas):
        scope = f" ({m['scope']})" if m.get("scope") else ""
        rows.append(f"| {m['name']} | `{slug}` | {m.get('tier', '')} | {m.get('access', '')}{scope} | {m.get('role', '')} |")
    parts = [
        body,
        "## The Crew roster\n\n"
        "Spawn organs as teammates using these agent types, named after the organ "
        "(teammate name `CoBbLE`, agent type `cobble`). Models are pinned by tier: do not override "
        "them upward without the Observer asking.\n\n"
        "| Organ | Agent type | Tier | Access | Job |\n|---|---|---|---|---|\n" + "\n".join(rows),
    ]
    state = latest_state(grimoire)
    if state:
        parts.append("## Where the Collective stands (from the session log, at launch)\n\n" + state)
    return "\n\n".join(parts) + "\n"


CREW_RULE = (
    "## Crew discipline\n\n"
    "Every organ is a full context window, so organs are expensive. Work alone by default. "
    "Call an organ only when the work is significant AND splits into independent parallel parts, "
    "or when you need a critic or verifier who did not write the code. One or two organs is the "
    "norm; never more than three at once. A small edit, a lookup, a single-file fix or a question "
    "is yours to do directly."
)


def build_ui_lead_prompt(personas, grimoire, crew):
    """Lead prompt for RaBbLE-UI. Organs are SDK subagents there (the Agent tool with
    `subagent_type` = the agent type), not agent-teams teammates. crew=False omits the
    roster entirely: the lead works solo and the UI passes no agents."""
    meta, body = personas[LEAD_SLUG]
    parts = [body]
    if crew:
        rows = []
        for slug, m, _ in organs(personas):
            scope = f" ({m['scope']})" if m.get("scope") else ""
            rows.append(f"| {m['name']} | `{slug}` | {m.get('tier', '')} | {m.get('access', '')}{scope} | {m.get('role', '')} |")
        parts.append(
            "## The Crew roster\n\n"
            "Organs run as subagents: call the Agent tool with `subagent_type` set to the agent type below. "
            "Models are pinned by tier: do not override them upward without the Observer asking. Ask each "
            "organ to put its full report in its final message.\n\n"
            "| Organ | Agent type | Tier | Access | Job |\n|---|---|---|---|---|\n" + "\n".join(rows))
        parts.append(CREW_RULE)
    else:
        parts.append("## Crew\n\nThe Crew is switched off for this session: work solo and do not spawn agents. "
                     "The Observer can switch it on with /crew on.")
    state = latest_state(grimoire)
    if state:
        parts.append("## Where the Collective stands (from the session log, at launch)\n\n" + state)
    return "\n\n".join(parts) + "\n"
