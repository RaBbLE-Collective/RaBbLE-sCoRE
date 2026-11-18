"""Where RaBbLE's eyes go: the Python twin of `mod gaze` in tui/rabble-tui/src/app.rs (same constants, same
semantics). The pointer wins while it moves; when it idles the entity picks what is most interesting (`choose`),
and with nothing to look at the face's own seeded saccades take over. A gaze never repaints by itself.

Pure logic, no Textual: the app feeds it the pointer, keys, and a scene, and applies what `Steer.step` returns.
TUNING: every knob Mark may want is a constant here (seconds, cells).
"""

from dataclasses import dataclass

EVERY = 0.100            # cadence of gaze updates
POINTER_ACTIVE = 2.0     # the pointer counts as active this long after its last motion
POINTER_TTL = 1.8        # ...and the target then lapses over this long
ATTN_EVERY = 0.5         # how often the entity re-chooses what to look at (low, so it never jitters)
ATTN_HOLD = 1.5          # how long it must stay on one thing before it may switch (hysteresis)
ATTN_TTL = 1.0
TYPING_HOLD = 2.0        # how long after the last keystroke the composer stays interesting
CARD_HOLD = 2.5          # ...and after a card changed, the card
RANGE_COLS = 40.0        # cells of offset from a face's centre that turn the eyes fully (x columns, y rows)
RANGE_ROWS = 12.0

MISS = object()   # "could not measure now" (distinct from None, "nothing to look at")

MODAL, TYPING, CARD, TEXT = "modal", "typing", "card", "text"   # most interesting first


@dataclass
class Scene:
    modal: tuple | None = None       # centre of the permission prompt
    typing: bool = False
    cursor: tuple = (0.0, 0.0)       # composer cursor cell
    card: bool = False
    streaming: bool = False
    newest: tuple | None = None      # the newest transcript line's end cell


def choose(s: Scene):
    """App state -> (focus, cell) worth looking at, or None."""
    if s.modal is not None:
        return MODAL, s.modal
    if s.typing:
        return TYPING, s.cursor
    if s.newest is not None:
        if s.card:
            return CARD, s.newest
        if s.streaming:
            return TEXT, s.newest
    return None


def aim(target, centre):
    """A screen cell seen from a face centre -> the face's normalized target."""
    return (max(-1.0, min(1.0, (target[0] - centre[0]) / RANGE_COLS)),
            max(-1.0, min(1.0, (target[1] - centre[1]) / RANGE_ROWS)))


class Steer:
    def __init__(self):
        self.pointer = None       # ((x, y), at)
        self.typed_at = None
        self.card_at = None
        self.cursor = (0, 0)
        self.modal = None
        self.newest = None        # the last good measurement of the newest line (screen cell): a measure that
                                  # fails mid-layout must not blind the eyes, so callers only overwrite it
        self.next = None
        self.attn_next = None
        self._cur = None          # (focus, cell)
        self._since = None
        self.aimed = []           # what was last sent to each face (None = handed back to the saccades)

    def current(self):
        return self._cur[1] if self._cur else None

    def pick(self, want, now):
        """Hysteresis: the same focus follows its target; a different one (or none) waits out ATTN_HOLD."""
        if self._cur and want and self._cur[0] == want[0]:
            self._cur = want
        elif self._cur is None or self._since is None or now - self._since >= ATTN_HOLD:
            self._cur, self._since = want, now
        return self.current()

    def within(self, stamp, hold, now):
        return stamp is not None and now - stamp < hold

    def step(self, now, scene, centres):
        """One gaze update. `scene` is a callable (only asked when the entity re-chooses); `centres` the face
        centres on screen, None for a face that is not showing. Returns (aimed per face, ttl), or None when it is
        not yet time."""
        if self.next is not None and now < self.next:
            return None
        self.next = now + EVERY
        pointer = self.pointer if self.pointer and now - self.pointer[1] < POINTER_ACTIVE else None
        if pointer:
            (x, y), _ = pointer
            target, ttl = (x + 0.5, y + 0.5), POINTER_TTL
        else:
            if self.attn_next is None or now >= self.attn_next:
                self.attn_next = now + ATTN_EVERY
                self.pick(choose(scene()), now)
            target, ttl = self.current(), ATTN_TTL
        self.aimed = [aim(target, c) if target is not None and c is not None else None for c in centres]
        return self.aimed, ttl
