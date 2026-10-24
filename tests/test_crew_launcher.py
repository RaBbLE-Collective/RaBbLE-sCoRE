"""The `rabble` Crew launcher (crew/rabble.py): persona parsing, the Claude Code
adapter, argument rules. No claude process is started.
Run: server/.venv/bin/python -m pytest tests"""

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "crew"))

import rabble  # noqa: E402

CREW_DIR = rabble.grimoire_root() / rabble.CREW_SUBDIR
EXPECTED = ["gobble", "quibble", "cobble", "crumble", "nibble", "archivist", "babble", "dabble"]


def write(path, text):
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture
def mini_crew(tmp_path):
    write(tmp_path / "_organ.md", "---\nname: organ-preamble\nslug: _organ\n---\nSHARED PREAMBLE\n")
    write(tmp_path / "rabble.md", "---\nname: RaBbLE\nslug: rabble\ntier: lead\n---\nLEAD BODY\n")
    write(tmp_path / "nibble.md", "---\nname: NiBbLE\nslug: nibble\norder: 2\nrole: reviews\n"
          "call_when: after builds\ntier: opus\naccess: read-only\n---\nNIBBLE BODY\n")
    write(tmp_path / "cobble.md", "---\nname: CoBbLE\nslug: cobble\norder: 1\nrole: builds\n"
          "tier: sonnet\naccess: full\n---\nCOBBLE BODY\n")
    write(tmp_path / "README.md", "# not a persona, no frontmatter\n")
    return tmp_path


@pytest.mark.skipif(not CREW_DIR.is_dir(), reason="RaBbLE-Grimoire not cloned beside sCoRE")
def test_canonical_crew_loads_in_roster_order():
    personas = rabble.load_crew(CREW_DIR)
    assert [s for s, _, _ in rabble.organs(personas)] == EXPECTED
    agents = rabble.build_agents(personas)
    assert list(agents) == EXPECTED
    assert agents["gobble"]["model"] == "haiku"
    assert agents["nibble"]["model"] == agents["quibble"]["model"] == "opus"
    for slug in ("gobble", "quibble", "nibble", "crumble"):
        assert "Edit" in agents[slug]["disallowedTools"]
        # the allowlist is what holds in tmux panes, where disallowedTools is ignored
        assert "Edit" not in agents[slug]["tools"] and "Write" not in agents[slug]["tools"]
    assert "tools" not in agents["cobble"] and "disallowedTools" not in agents["cobble"]


def test_organ_prompt_is_preamble_then_body(mini_crew):
    agents = rabble.build_agents(rabble.load_crew(mini_crew))
    prompt = agents["nibble"]["prompt"]
    assert prompt.index("SHARED PREAMBLE") < prompt.index("NIBBLE BODY")
    assert agents["nibble"]["description"] == "NiBbLE: reviews. after builds"
    assert list(agents) == ["cobble", "nibble"]  # order field, not filename


def test_lead_and_preamble_are_not_agents(mini_crew):
    agents = rabble.build_agents(rabble.load_crew(mini_crew))
    assert "rabble" not in agents and "_organ" not in agents


def test_missing_lead_is_an_error(mini_crew):
    (mini_crew / "rabble.md").unlink()
    with pytest.raises(ValueError, match="rabble.md"):
        rabble.load_crew(mini_crew)


def test_unknown_access_is_an_error(mini_crew):
    write(mini_crew / "cobble.md", "---\nname: CoBbLE\nslug: cobble\naccess: root\n---\nx\n")
    with pytest.raises(ValueError, match="unknown access"):
        rabble.build_agents(rabble.load_crew(mini_crew))


def test_persona_without_frontmatter_is_an_error(mini_crew):
    write(mini_crew / "stray.md", "no frontmatter here\n")
    with pytest.raises(ValueError, match="missing frontmatter"):
        rabble.load_crew(mini_crew)


def test_crlf_and_bom_persona_parses(mini_crew):
    (mini_crew / "nibble.md").write_bytes(
        "\ufeff---\r\nname: NiBbLE\r\nslug: nibble\r\naccess: read-only\r\n---\r\nBODY\r\n".encode("utf-8"))
    meta, body = rabble.load_crew(mini_crew)["nibble"]
    assert meta["name"] == "NiBbLE" and body == "BODY"


def test_duplicate_slug_is_an_error(mini_crew):
    write(mini_crew / "copy.md", "---\nname: Other\nslug: cobble\n---\nx\n")
    with pytest.raises(ValueError, match="duplicate slug"):
        rabble.load_crew(mini_crew)


def test_lead_prompt_carries_roster_and_latest(mini_crew, tmp_path):
    grimoire = tmp_path / "g"
    (grimoire / "log").mkdir(parents=True)
    write(grimoire / "log" / "SESSION-LOG.md",
          "# log\n\n---\n\n## LATEST — S999\n\n**Next:** thing\n\n---\n\n## older\n")
    prompt = rabble.build_lead_prompt(rabble.load_crew(mini_crew), grimoire)
    assert prompt.startswith("LEAD BODY")
    assert "| NiBbLE | `nibble` | opus | read-only |" in prompt
    assert "## LATEST — S999" in prompt and "## older" not in prompt


def test_latest_state_absent_log_is_empty(tmp_path):
    assert rabble.latest_state(tmp_path) == ""


def test_positional_words_are_reserved(capsys):
    with pytest.raises(SystemExit) as e:
        rabble.split_args(["status"])
    assert e.value.code == 2
    assert "reserved" in capsys.readouterr().err


def test_word_after_a_bare_flag_is_still_reserved():
    with pytest.raises(SystemExit):
        rabble.split_args(["--verbose", "status"])


def test_headless_is_refused(capsys):
    for flag in ("-p", "--print"):
        with pytest.raises(SystemExit):
            rabble.split_args([flag, "x"])
    assert "interactive" in capsys.readouterr().err


def test_prompt_files_are_per_launch_and_private(tmp_path):
    a = rabble.write_prompt_file(tmp_path, "A")
    b = rabble.write_prompt_file(tmp_path, "B")
    assert a != b and a.read_text() == "A" and b.read_text() == "B"
    assert a.stat().st_mode & 0o777 == 0o600


def test_old_prompt_files_are_pruned(tmp_path):
    old = tmp_path / "crew-lead-old.md"
    old.write_text("x")
    os.utime(old, (0, 0))
    rabble.write_prompt_file(tmp_path, "new")
    assert not old.exists()


def test_flags_and_double_dash_pass_through():
    assert rabble.split_args(["--model", "opus"]) == (None, ["--model", "opus"])
    assert rabble.split_args(["--add-dir", "../x", "--verbose"]) == (None, ["--add-dir", "../x", "--verbose"])
    assert rabble.split_args(["--", "fix the beam"]) == (None, ["fix the beam"])
    assert rabble.split_args(["--crew-list"]) == ("--crew-list", [])


def test_claude_argv_adds_teammate_mode_once(tmp_path, monkeypatch):
    monkeypatch.delenv("RABBLE_TEAMMATE_MODE", raising=False)
    argv = rabble.claude_argv(tmp_path / "p.md", {"a": {}}, ["--model", "opus"])
    assert argv[:2] == ["claude", "--append-system-prompt-file"]
    assert argv.count("--teammate-mode") == 1 and argv[-2:] == ["--model", "opus"]
    assert argv[argv.index("--teammate-mode") + 1] == "in-process"
    argv = rabble.claude_argv(tmp_path / "p.md", {}, ["--teammate-mode", "tmux"])
    assert argv.count("--teammate-mode") == 1 and argv[-1] == "tmux"
