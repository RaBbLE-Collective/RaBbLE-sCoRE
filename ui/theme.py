"""RaBbLE-UI colours: the palette, the Textual theme built from it, and the Crew's organ colours.

Every colour is read at startup from RaBbLE-Grimoire (RaBbLE-Palette.md and the Crew persona
frontmatter). No hex lives in ui/ code: the palette is the only source.

Env: RABBLE_GRIMOIRE (default: sibling of this repo).
"""

import importlib.util
import re
from functools import cache
from pathlib import Path

from textual.theme import Theme

SCORE_ROOT = Path(__file__).resolve().parent.parent
PALETTE_REL = Path("RaBbLE-Agent") / "RaBbLE-Palette.md"


@cache
def _roster():
    """crew/ has no __init__, so load roster.py by path (one resolution of the Grimoire)."""
    spec = importlib.util.spec_from_file_location("rabble_crew_roster", SCORE_ROOT / "crew" / "roster.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def grimoire_root() -> Path:
    return _roster().grimoire_root()


@cache
def _load(path: str) -> dict[str, str]:
    text = Path(path).read_text(encoding="utf-8")
    return {k: v.lower() for k, v in re.findall(r'^\s+(\w+):\s+"(#[0-9a-fA-F]{6})"', text, re.M)}


def load_palette(path=None) -> dict[str, str]:
    """name -> "#rrggbb" from the YAML block of RaBbLE-Palette.md."""
    return dict(_load(str(path or grimoire_root() / PALETTE_REL)))


@cache
def rabble_theme() -> Theme:
    p = load_palette()
    return Theme(
        name="rabble",
        primary=p["magenta"],
        secondary=p["cyan"],
        accent=p["violet"],
        background=p["bg"],
        surface=p["surface"],
        panel=p.get("raised", p["surface"]),
        foreground=p["text"],
        error=p.get("red", p["magenta"]),
        warning=p.get("yellow", p["pink"]),
        success=p.get("green", p["cyan"]),
        dark=True,
    )


@cache
def _organ_colors() -> dict[str, str]:
    p = load_palette()
    colors = {}
    try:
        roster = _roster()
        personas = roster.load_crew(grimoire_root() / roster.CREW_SUBDIR)
    except (OSError, ValueError):
        return colors
    for slug, (meta, _) in personas.items():
        if slug == roster.PREAMBLE_SLUG:
            continue
        # The Archivist's colour is the void itself; it shows as canonical muted text.
        colors[slug] = p["muted"] if slug == "archivist" else meta.get("color_base", p["text"])
    return colors


def organ_colors() -> dict[str, str]:
    """Crew persona slug -> hex (the lead 'rabble' included)."""
    return dict(_organ_colors())


def organ_color(slug: str) -> str:
    return _organ_colors().get(slug, load_palette()["text"])
