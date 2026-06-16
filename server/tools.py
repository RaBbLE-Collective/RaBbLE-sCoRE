import httpx
import os
import logging
from pathlib import Path

logger = logging.getLogger("rabble.tools")

# Remote Grimoire endpoint — set GRIMOIRE_URL in env to enable.
# Expected: GET {GRIMOIRE_URL}/gist/{filename} → 200 text/markdown
# Falls back to local gist directory when unset or unreachable.
GRIMOIRE_URL = os.getenv("GRIMOIRE_URL", "").strip()

_DOC_MAP: dict[str, str] = {
    "identity":        "RaBbLE-Identity-gist.md",
    "collective":      "RaBbLE-Collective-gist.md",
    "roadmap":         "RaBbLE-Roadmap-gist.md",
    "episode1":        "RaBbLE-Episode1-gist.md",
    "versioning":      "RaBbLE-Versioning-gist.md",
    "palette":         "RaBbLE-Palette-gist.md",
    "commit-style":    "RaBbLE-CommitStyle-gist.md",
    "integration-map": "RaBbLE-Integration-Map-gist.md",
    "overview":        "RaBbLE-Collective-Overview-gist.md",
}

FETCH_GRIMOIRE_TOOL = {
    "type": "function",
    "function": {
        "name": "fetch_grimoire",
        "description": (
            "Fetch a specific Grimoire document when you need accurate details about RaBbLE — "
            "architecture, versioning, roadmap, palette, episode status, or identity. "
            "Use this instead of guessing when the user asks something specific. "
            "Only call when the question requires knowledge beyond your current context."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "doc": {
                    "type": "string",
                    "description": "Document slug. Choose the most relevant one.",
                    "enum": sorted(_DOC_MAP.keys()),
                }
            },
            "required": ["doc"],
        },
    },
}

ALL_TOOLS = [FETCH_GRIMOIRE_TOOL]


def _find_local_gist_dir() -> Path | None:
    env_path = os.getenv("GRIMOIRE_PATH", "").strip()
    if env_path:
        p = Path(env_path) / "gist"
        return p if p.exists() else None
    server_dir = Path(__file__).resolve().parent
    candidates = [
        server_dir.parent.parent / "RaBbLE-Grimoire" / "gist",
        server_dir.parent / "RaBbLE-Grimoire" / "gist",
    ]
    for c in candidates:
        if c.exists():
            return c
    return None


async def execute_tool(name: str, arguments: dict) -> str:
    if name == "fetch_grimoire":
        return await _fetch_grimoire(arguments.get("doc", ""))
    return f"[unknown tool: {name}]"


async def _fetch_grimoire(doc: str) -> str:
    filename = _DOC_MAP.get(doc)
    if not filename:
        return f"[grimoire: unknown doc '{doc}' — available: {', '.join(sorted(_DOC_MAP))}]"

    if GRIMOIRE_URL:
        url = GRIMOIRE_URL.rstrip("/") + f"/gist/{filename}"
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(url, timeout=8)
                if resp.status_code == 200:
                    logger.info("grimoire tool: fetched %s from %s", doc, GRIMOIRE_URL)
                    return resp.text
                logger.warning("grimoire tool: remote %s → %s, falling back", url, resp.status_code)
        except Exception as exc:
            logger.warning("grimoire tool: remote fetch failed (%s), falling back to local", exc)

    gist_dir = _find_local_gist_dir()
    if gist_dir:
        p = gist_dir / filename
        if p.exists():
            logger.info("grimoire tool: serving %s from local gist", doc)
            return p.read_text().strip()

    return (
        f"[grimoire: '{doc}' unavailable — "
        f"set GRIMOIRE_URL to the hosted endpoint or run locally with the Grimoire present]"
    )
