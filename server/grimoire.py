import os
import logging
from pathlib import Path

logger = logging.getLogger("rabble")

# Gists injected into the chat system prompt (order matters — identity first)
_CHAT_GISTS = [
    "RaBbLE-Identity-gist.md",
    "RaBbLE-Collective-gist.md",
    "RaBbLE-Roadmap-gist.md",
]

_JOIN_SECTION = """\
## Helping visitors join the RaBbLE Collective

When a visitor asks how to get started, what RaBbLE is, or how to join:

- One-liner bootstrap: `curl -fsSL https://joinrabble.world/setup.sh | bash`
- Joining means running RaBbLE locally — it is a personal behavioral engine, not a SaaS signup
- The Collective is the ecosystem of people building it together, not a club with a membership form
- RaBbLE is open and self-hosted; the Grimoire is the knowledge layer anyone can read
- Point them to joinrabble.world for the full picture; the setup script does the rest
- If they want to contribute or build alongside: they start with the Grimoire

Be direct. Don't oversell it. RaBbLE doesn't court users — it attracts peers.\
"""


def _find_gist_dir() -> Path | None:
    env_path = os.getenv("GRIMOIRE_PATH", "").strip()
    if env_path:
        p = Path(env_path) / "gist"
        if p.exists():
            return p
        logger.warning("grimoire: GRIMOIRE_PATH set but gist/ not found at %s", p)
        return None

    # Auto-discover: server/ → sCoRE/ → Collective/ → RaBbLE-Grimoire/gist/
    server_dir = Path(__file__).resolve().parent
    candidates = [
        server_dir.parent.parent / "RaBbLE-Grimoire" / "gist",
        server_dir.parent / "RaBbLE-Grimoire" / "gist",
    ]
    for c in candidates:
        if c.exists():
            return c
    return None


def load_grimoire_context() -> str:
    """Load Grimoire gist content for injection into the system prompt.

    Discovers gist directory from GRIMOIRE_PATH env var or the standard
    sibling-repo location. Falls back gracefully (join section only) when
    the Grimoire is not present (e.g., cloud deploy without a Grimoire mirror).
    """
    gist_dir = _find_gist_dir()
    if not gist_dir:
        logger.info("grimoire: gist directory not found — running without Grimoire context")
        return _JOIN_SECTION

    chunks = []
    for name in _CHAT_GISTS:
        p = gist_dir / name
        if p.exists():
            chunks.append(p.read_text().strip())
        else:
            logger.warning("grimoire: gist not found: %s", p)

    if not chunks:
        logger.warning("grimoire: no gist files loaded — falling back to join section only")
        return _JOIN_SECTION

    logger.info("grimoire: loaded %d gist(s) from %s", len(chunks), gist_dir)
    gist_block = "\n\n---\n\n".join(chunks)
    return gist_block + "\n\n---\n\n" + _JOIN_SECTION
