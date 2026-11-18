"""PermissionScreen and SessionPicker driven headless through App.run_test().
Run: ~/.local/share/RaBbLE/rabble-ui/bin/python -m pytest tests"""

import asyncio
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest  # noqa: E402

pytest.importorskip("textual")  # RaBbLE-UI venv only; the server venv skips

from textual.app import App  # noqa: E402
from textual.widgets import Static  # noqa: E402

from orchestra.events import PermissionAsk, PermissionReply  # noqa: E402
from orchestra.sessions import SessionRow  # noqa: E402
from ui.widgets import sessions as sess_mod  # noqa: E402
from ui.widgets.permission import PermissionScreen  # noqa: E402
from ui.widgets.sessions import SessionPicker  # noqa: E402

ROOT = os.path.join(os.path.dirname(__file__), "..")


class Host(App):
    def __init__(self, screen):
        super().__init__()
        self._screen = screen
        self.result = "unset"

    def on_mount(self):
        self.push_screen(self._screen, lambda r: setattr(self, "result", r))


def drive(screen, keys, text=None):
    async def go():
        app = Host(screen)
        async with app.run_test() as pilot:
            await pilot.pause()
            for k in keys:
                await pilot.press(k)
                await pilot.pause()
            if text is not None:
                await pilot.pause(0.3)
        return app.result
    return asyncio.run(go())


@pytest.fixture(autouse=True)
def armed_now(monkeypatch):
    """Tests press keys at once; the arming delay has its own test."""
    monkeypatch.setattr(PermissionScreen, "ARM_SECONDS", 0.0)


def ask(tool="Bash", **inp):
    return PermissionAsk(id="1", organ="rabble", tool=tool, input=inp)


def test_allow_keys():
    assert drive(PermissionScreen(ask(command="ls")), ["y"]) == PermissionReply("allow")
    assert drive(PermissionScreen(ask(command="ls")), ["1"]) == PermissionReply("allow")


def test_allow_session():
    assert drive(PermissionScreen(ask(command="ls")), ["a"]) == PermissionReply("allow_session")


def test_deny_with_reason():
    r = drive(PermissionScreen(ask(command="ls")), ["n", *"too risky", "enter"])
    assert r == PermissionReply("deny", "too risky")


def test_deny_empty_reason():
    assert drive(PermissionScreen(ask(command="ls")), ["n", "enter"]) == PermissionReply("deny", "")


def test_escape_denies():
    assert drive(PermissionScreen(ask(command="ls")), ["escape"]) == PermissionReply("deny")


def body_text(screen, keys=()):
    async def go():
        app = Host(screen)
        async with app.run_test() as pilot:
            await pilot.pause()
            return str(app.screen.query_one("#perm-body", Static).render())
    return asyncio.run(go())


def test_bash_command_not_truncated():
    cmd = "echo " + "x" * 5000
    assert cmd in body_text(PermissionScreen(ask(command=cmd)))


def test_edit_diff():
    text = body_text(PermissionScreen(ask("Edit", file_path="/a.py", old_string="one\n", new_string="two\n")))
    lines = text.splitlines()
    assert "/a.py" in lines[0]
    assert "-one" in lines and "+two" in lines


def test_organ_label():
    a = PermissionAsk(id="1", organ="gobble", tool="Bash", input={"command": "ls"})

    async def go():
        app = Host(PermissionScreen(a))
        async with app.run_test() as pilot:
            await pilot.pause()
            return str(app.screen.query_one("#perm-who", Static).render())
    assert "gobble" in asyncio.run(go())


ROWS = [SessionRow("s-new", "newest work", 1e9, "/p", None), SessionRow("s-old", "older", 1e8, "/p", 4)]


def test_picker_enter(monkeypatch):
    monkeypatch.setattr(sess_mod, "recent_sessions", lambda cwd: ROWS)
    assert drive(SessionPicker("/p"), ["enter"], text="w") == "s-new"


def test_picker_down_enter(monkeypatch):
    monkeypatch.setattr(sess_mod, "recent_sessions", lambda cwd: ROWS)
    assert drive(SessionPicker("/p"), ["down", "enter"], text="w") == "s-old"


def test_picker_escape(monkeypatch):
    monkeypatch.setattr(sess_mod, "recent_sessions", lambda cwd: ROWS)
    assert drive(SessionPicker("/p"), ["escape"], text="w") is None


def test_picker_empty(monkeypatch):
    monkeypatch.setattr(sess_mod, "recent_sessions", lambda cwd: [])

    async def go():
        app = Host(SessionPicker("/p"))
        async with app.run_test() as pilot:
            await pilot.pause(0.3)
            txt = str(app.screen.query_one("#sess-empty", Static).render())
            await pilot.press("escape")
            await pilot.pause()
        return txt, app.result
    txt, res = asyncio.run(go())
    assert "No earlier sessions here" in txt and res is None


def test_no_hex_literals():
    for f in ("widgets/permission.py", "widgets/sessions.py"):
        src = open(os.path.join(ROOT, "ui", f)).read()
        assert not re.search(r"#[0-9a-fA-F]{6}\b", src), f


def test_keys_inside_the_arming_window_are_ignored(monkeypatch):
    # a keystroke meant for the composer must not answer an ask nobody has read
    monkeypatch.setattr(PermissionScreen, "ARM_SECONDS", 30.0)

    async def go():
        app = Host(PermissionScreen(ask(command="rm -rf build")))
        async with app.run_test() as pilot:
            await pilot.pause()
            for k in ("a", "y", "n"):
                await pilot.press(k)
            await pilot.pause()
            early = app.result
            await pilot.press("escape")  # deny is never held back
            await pilot.pause()
        return early, app.result
    assert asyncio.run(go()) == ("unset", PermissionReply("deny"))


@pytest.mark.parametrize("rows", [40, 24, 16, 10, 8])
def test_answer_controls_stay_on_screen_in_a_short_pane(rows):
    """Tiled panes can be a dozen rows tall: the buttons and the deny reason must still fit."""
    ask = PermissionAsk(id="p", organ="lead", tool="Bash", input={"command": "git push origin main\n" * 30},
                        title="Claude wants to run: git push", description="Push the branch.")

    async def go():
        app = Host(PermissionScreen(ask))
        async with app.run_test(size=(100, rows)) as pilot:
            await pilot.pause(0.6)
            box = app.screen.query_one("#perm-box").content_region  # inside border and padding
            for b in app.screen.query("Button"):
                assert box.y <= b.region.y and b.region.bottom <= box.bottom, (rows, b.id, b.region, box)
            await pilot.click("#perm-session")
            await pilot.pause(0.2)
        assert app.result.decision == "allow_session"

        app = Host(PermissionScreen(ask))
        async with app.run_test(size=(100, rows)) as pilot:
            await pilot.pause(0.6)
            await pilot.press("n")
            await pilot.pause(0.3)
            r = app.screen.query_one("#perm-reason")
            box = app.screen.query_one("#perm-box").content_region
            assert r.region.height > 0 and r.region.bottom <= box.bottom, (rows, r.region, box)
    asyncio.run(go())
