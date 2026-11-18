"""SplitMix64: the face's one portable PRNG.

Python's `random.Random` (Mersenne Twister) cannot be reproduced in Rust without re-implementing its
`random()/uniform/choice/sample`, so the face's decorative randomness (mesh layout, orb dust, glitch, blink
and gaze) uses this instead, in both bodies: `tui/rabble-face/src/prng.rs` is the same algorithm and the
same helper definitions, pinned by one known-answer test on each side (tests/test_ui_face.py and the Rust
`prng::tests`). Keep the two in step; every helper is defined here in terms of `next_u64()` only.
"""

MASK = 0xFFFFFFFFFFFFFFFF
GAMMA = 0x9E3779B97F4A7C15


class SplitMix64:
    __slots__ = ("state",)

    def __init__(self, seed=0):
        self.state = seed & MASK   # negative seeds wrap (two's complement), like Rust `seed as u64`

    def next_u64(self):
        self.state = (self.state + GAMMA) & MASK
        z = self.state
        z = ((z ^ (z >> 30)) * 0xBF58476D1CE4E5B9) & MASK
        z = ((z ^ (z >> 27)) * 0x94D049BB133111EB) & MASK
        return z ^ (z >> 31)

    def random(self):
        """[0, 1): the top 53 bits."""
        return (self.next_u64() >> 11) * (1.0 / 9007199254740992.0)

    def uniform(self, a, b):
        return a + (b - a) * self.random()

    def randrange(self, n):
        """0 <= i < n, as floor(random() * n)."""
        return int(self.random() * n)

    def randint(self, a, b):
        """a <= i <= b."""
        return a + self.randrange(b - a + 1)

    def choice(self, seq):
        return seq[self.randrange(len(seq))]

    def sample(self, population, k):
        """k distinct items: a partial Fisher-Yates over a copy (item i swaps with i + randrange(n - i))."""
        pool = list(population)
        n = len(pool)
        for i in range(min(k, n)):
            j = i + self.randrange(n - i)
            pool[i], pool[j] = pool[j], pool[i]
        return pool[:k]
