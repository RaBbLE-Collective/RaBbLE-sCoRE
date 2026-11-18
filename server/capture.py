"""
Capture turns for the beam: turn a transcript into a markdown note.

Pure functions, no I/O, so they test without a socket. beam.py does the
speech-to-text and the LLM call; this module builds the prompt, reads the
answer, assembles the file and cuts it into wire-sized parts.
Contract: RaBbLE-Grimoire/RaBbLE-Pocket/RaBbLE-Pocket-Beam-Protocol.md#capture-turns.
"""

import json
import re
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

PART_BYTES = 8192
TITLE_MAX = 60
TAGS_MAX = 5
_SAFE_ID = re.compile(r"[a-z0-9-]{1,64}")

SHAPE_SYSTEM = """You turn a spoken or typed capture into a tidy note. You are given the \
raw transcript (speech recognition output, so it may contain errors).

Reply with ONE JSON object and nothing else (no markdown fence, no commentary):
{"title": string, "tags": [string], "summary": [string], "notes": string}

Rules:
- Use ONLY what is in the transcript. Never add facts, names, numbers, dates, \
conclusions or advice that are not there. If something is unclear, leave it out.
- title: at most 60 characters, plain words, no quotes or trailing punctuation.
- tags: at most 5, lower-case kebab-case, only subjects actually discussed.
- summary: short bullet strings (no leading dash), the main points only. An empty list is fine for a short capture.
- notes: the capture cleaned up and structured as markdown (short paragraphs, lists where the speaker listed things). Keep the speaker's own words where they matter. Fix obvious speech-recognition slips only when the intent is certain. No headings."""


def build_prompt(transcript: str, source: str) -> tuple[str, list[dict]]:
    """(system, messages) for the one shaping call."""
    kind = "spoken aloud" if source == "voice" else "typed"
    user = f"Transcript ({kind}):\n\n{transcript}"
    return SHAPE_SYSTEM, [{"role": "user", "content": user}]


def _clean_title(title: str) -> str:
    title = re.sub(r"\s+", " ", title).strip().strip("\"'").rstrip(".:;,")
    if len(title) > TITLE_MAX:
        title = title[:TITLE_MAX].rsplit(" ", 1)[0] or title[:TITLE_MAX]
    return title.strip()


def _clean_tag(tag) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(tag).lower()).strip("-")[:30]


def parse_shaped(raw: str) -> dict | None:
    """The model's JSON, normalised; None when it isn't usable."""
    raw = (raw or "").strip()
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw)   # models fence it anyway
    try:
        data = json.loads(raw)
    except ValueError:
        start, end = raw.find("{"), raw.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            data = json.loads(raw[start:end + 1])
        except ValueError:
            return None
    if not isinstance(data, dict):
        return None
    title = _clean_title(str(data.get("title") or ""))
    if not title:
        return None
    tags = []
    for tag in data.get("tags") or []:
        tag = _clean_tag(tag)
        if tag and tag not in tags:
            tags.append(tag)
    summary = data.get("summary") or []
    if isinstance(summary, str):
        summary = [summary]
    summary = [re.sub(r"^[-*\s]+", "", str(s)).strip() for s in summary]
    notes = data.get("notes") or ""
    if not isinstance(notes, str):
        notes = "\n".join(str(n) for n in notes) if isinstance(notes, list) else str(notes)
    return {"title": title, "tags": tags[:TAGS_MAX],
            "summary": [s for s in summary if s], "notes": notes.strip()}


def fallback_title(transcript: str) -> str:
    words = transcript.split()
    title = ""
    for word in words:
        if len(f"{title} {word}".strip()) > TITLE_MAX:
            break
        title = f"{title} {word}".strip()
    return _clean_title(title or transcript[:TITLE_MAX]) or "Untitled capture"


def safe_id(raw, started, tz: str | None, turn=0) -> str:
    """The device's note id when it's safe as a file name, else one made here
    in the same shape with device suffix "x"."""
    if isinstance(raw, str) and _SAFE_ID.fullmatch(raw):
        return raw
    local = local_time(started, tz)
    return f"{local:%Y%m%d-%H%M}-x-{int(turn) if str(turn).lstrip('-').isdigit() else 0}"


def local_time(started, tz: str | None) -> datetime:
    """`started` (UTC seconds; missing or bad means now) in zone `tz` (unknown means UTC)."""
    try:
        secs = float(started)
        if secs <= 0:
            raise ValueError
    except (TypeError, ValueError):
        secs = time.time()
    try:
        zone = ZoneInfo(str(tz)) if tz else timezone.utc
    except Exception:   # ZoneInfoNotFoundError, ValueError on odd keys
        zone = timezone.utc
    try:
        return datetime.fromtimestamp(secs, zone).replace(microsecond=0)
    except (OverflowError, OSError, ValueError):
        return datetime.now(timezone.utc).replace(microsecond=0)


def note_path(note_id: str, created: datetime) -> str:
    return f"{created:%Y/%m}/{note_id}.md"


def _yaml_str(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)   # a JSON string is a valid YAML one


def assemble_note(note_id: str, created: datetime, device: str, source: str,
                  duration_s: int | None, transcript: str, shaped: dict | None) -> str:
    """The markdown file. The transcript goes in verbatim; Summary and Notes
    appear only when the shaping call gave something."""
    title = shaped["title"] if shaped else fallback_title(transcript)
    tags = shaped["tags"] if shaped else []
    head = ["---", f"id: {note_id}", "type: pocket-note", f"title: {_yaml_str(title)}",
            f"created: {created.isoformat()}", f"device: {_yaml_str(device)}", f"source: {source}"]
    if source == "voice" and duration_s is not None:
        head.append(f"duration_s: {int(duration_s)}")
    head.append(f"tags: [{', '.join(tags)}]")
    head.append("---")
    out = ["\n".join(head)]
    if shaped and shaped["summary"]:
        out.append("## Summary\n" + "\n".join(f"- {s}" for s in shaped["summary"]))
    if shaped and shaped["notes"]:
        out.append("## Notes\n" + shaped["notes"])
    out.append("## Transcript\n" + transcript)
    return "\n\n".join(out) + "\n"


def note_title(md: str) -> str:
    """The title back out of an assembled note (for the `note` frame)."""
    m = re.search(r"^title: (.*)$", md, re.M)
    if not m:
        return ""
    try:
        return json.loads(m.group(1))
    except ValueError:
        return m.group(1)


def split_parts(md: str, limit: int = PART_BYTES) -> list[str]:
    """Cut into parts of at most `limit` UTF-8 bytes, never inside a character."""
    data = md.encode("utf-8")
    parts, i = [], 0
    while i < len(data):
        end = min(i + limit, len(data))
        while end < len(data) and (data[end] & 0xC0) == 0x80:   # mid-character: back up
            end -= 1
        parts.append(data[i:end].decode("utf-8"))
        i = end
    return parts or [""]
