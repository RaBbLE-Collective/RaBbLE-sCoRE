import re
from pathlib import Path

import pytest

pytest.importorskip("textual")  # RaBbLE-UI venv only; the server venv skips

from ui import theme

UI = Path(theme.__file__).parent
HEX = re.compile(r"#[0-9a-fA-F]{6}")
needs_grimoire = pytest.mark.skipif(not (theme.grimoire_root() / theme.PALETTE_REL).exists(), reason="no Grimoire")


@needs_grimoire
def test_palette_parses():
    p = theme.load_palette()
    for key in ("bg", "text", "cyan", "magenta", "violet", "muted"):
        assert re.fullmatch(r"#[0-9a-f]{6}", p[key])


@needs_grimoire
def test_rabble_theme():
    t = theme.rabble_theme()
    p = theme.load_palette()
    assert t.name == "rabble" and t.dark
    assert (t.primary, t.secondary, t.background) == (p["magenta"], p["cyan"], p["bg"])


def test_no_hex_literals():
    for name in ("theme.py", "face.py"):
        assert not HEX.search((UI / name).read_text()), name


@needs_grimoire
def test_organ_colors():
    c = theme.organ_colors()
    slugs = {"rabble", "archivist", "babble", "cobble", "crumble", "dabble", "gobble", "nibble", "quibble"}
    assert slugs <= set(c)
    assert c["archivist"] == theme.load_palette()["muted"]
    assert theme.organ_color("nope") == theme.load_palette()["text"]
