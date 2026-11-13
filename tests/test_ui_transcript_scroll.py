"""Transcript scroll: follow while at the bottom, stay put when the reader scrolled up,
and never get stranded at the top by a layout jump while blurred or just after refocus."""

import asyncio

from textual.app import App

from ui.widgets.transcript import Transcript

LONG = "\n\n".join(f"paragraph {i} with some `code` text" for i in range(120))


class _App(App):
    def compose(self):
        self.t = Transcript(lambda organ: "white")
        yield self.t


def _run(coro):
    return asyncio.run(coro)


async def _filled(p, app):
    await app.t.user("hi")
    await app.t.delta("lead", LONG)
    await p.pause(0.5)
    assert app.t.max_scroll_y > 50 and app.t.scroll_y == app.t.max_scroll_y


def test_stray_jump_to_top_while_blurred_is_repinned():
    async def go():
        app = _App()
        async with app.run_test(size=(80, 24)) as p:
            await _filled(p, app)
            t = app.t
            t.remember_view()
            t.scroll_to(y=0, animate=False)  # layout stranding, not the reader
            await p.pause(0.5)
            await p.pause(0.5)
            assert t.max_scroll_y - t.scroll_y <= 1  # still blurred: nothing else would re-pin it
            for i in range(5):
                await t.delta("lead", f"\n\nmore {i}")
            t.restore_view()
            await p.pause(0.5)
            assert t.max_scroll_y - t.scroll_y <= 1
    _run(go())


def test_stray_jump_right_after_refocus_is_repinned():
    async def go():
        app = _App()
        async with app.run_test(size=(80, 24)) as p:
            await _filled(p, app)
            t = app.t
            t.remember_view()
            t.restore_view()
            await p.pause(0.4)  # past restore_view's own re-pin timers
            t.scroll_to(y=0, animate=False)
            await p.pause(0.5)
            assert t.max_scroll_y - t.scroll_y <= 1
    _run(go())


def test_reader_scrolled_up_is_not_dragged_down():
    async def go():
        app = _App()
        async with app.run_test(size=(80, 24)) as p:
            await _filled(p, app)
            t = app.t
            t.scroll_to(y=10, animate=False)
            await p.pause(0.2)
            for i in range(10):
                await t.delta("lead", f"\n\nmore {i}")
                await p.pause(0.02)
            assert abs(t.scroll_y - 10) < 2
    _run(go())


def test_nudged_two_lines_up_still_follows():
    async def go():
        app = _App()
        async with app.run_test(size=(80, 24)) as p:
            await _filled(p, app)
            t = app.t
            t.scroll_to(y=t.max_scroll_y - 2, animate=False)
            await p.pause(0.2)
            await t.block("lead", LONG + "\n\nfinal")
            await p.pause(0.6)
            assert t.max_scroll_y - t.scroll_y <= 1
    _run(go())


def test_submit_jumps_to_bottom_after_scrolling_up():
    async def go():
        app = _App()
        async with app.run_test(size=(80, 24)) as p:
            await _filled(p, app)
            t = app.t
            t.scroll_to(y=5, animate=False)
            await p.pause(0.2)
            await t.user("again")
            await p.pause(0.5)
            assert t.max_scroll_y - t.scroll_y <= 1
    _run(go())


def test_wheel_burst_accumulates_into_one_glide():
    from textual import events

    async def go():
        app = _App()
        async with app.run_test(size=(80, 24)) as p:
            await _filled(p, app)
            t = app.t
            top = t.scroll_y
            for _ in range(4):
                t.post_message(events.MouseScrollUp(t, 1, 1, 0, 0, 0, False, False, False))
            await p.pause(0.05)
            assert t._glide_target is not None and t._glide_target < top   # notches moved the target
            mid = t.scroll_y
            assert t._glide_target < mid <= top                            # the view is still gliding, not jumped
            ys = []
            for _ in range(20):
                await p.pause(0.02)
                ys.append(t.scroll_y)
            assert all(a >= b for a, b in zip(ys, ys[1:]))                 # monotonic: no ratchet back-steps
            await p.pause(0.5)
            assert t._glide_target is None and t.scroll_y == top - 4 * t.WHEEL_LINES
    _run(go())
