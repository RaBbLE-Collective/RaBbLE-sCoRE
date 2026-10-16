import httpx
import os
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

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

GET_TIME_TOOL = {
    "type": "function",
    "function": {
        "name": "get_time",
        "description": (
            "The current date and time. Call it whenever the answer depends on what time or day it is now; "
            "never guess. Give an IANA time zone (e.g. America/New_York) when you know where the user is."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "tz": {"type": "string", "description": "IANA time zone name; omit for UTC."}
            },
        },
    },
}

ALL_TOOLS = [FETCH_GRIMOIRE_TOOL, GET_TIME_TOOL]


def server_time(tz: str | None = None) -> dict:
    """sCoRE's clock, the one time source for every client: the Pocket's beam `ready`,
    GET /api/v1/time, and the get_time tool. `time` is UTC seconds since the epoch.
    With a valid IANA zone, also that zone's local time and offset; raises
    ZoneInfoNotFoundError (a KeyError) on an unknown one."""
    now = time.time()
    out = {"time": round(now, 3), "utc": datetime.fromtimestamp(now, timezone.utc).isoformat(timespec="seconds")}
    if tz:
        local = datetime.fromtimestamp(now, ZoneInfo(tz))
        out.update(tz=tz, local=local.isoformat(timespec="seconds"),
                   offset_s=int(local.utcoffset().total_seconds()), abbr=local.tzname())
    return out


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
    if name == "get_time":
        try:
            t = server_time(arguments.get("tz") or None)
        except (ZoneInfoNotFoundError, ValueError):
            t = server_time()
        return t.get("local", t["utc"]) + (f" ({t['tz']})" if "tz" in t else " (UTC)")
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
