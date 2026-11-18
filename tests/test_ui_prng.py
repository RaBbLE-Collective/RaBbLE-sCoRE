"""Known answers for ui/prng.py. The same numbers are asserted by tui/rabble-face/src/prng.rs."""

from ui.prng import SplitMix64


def test_splitmix64_reference_values():
    # seed 0 is the published SplitMix64 reference stream
    r = SplitMix64(0)
    assert [r.next_u64() for _ in range(3)] == [0xe220a8397b1dcdaf, 0x6e789e6aa1b965f4, 0x06c45d188009454f]
    r = SplitMix64(1234567)
    assert [r.next_u64() for _ in range(3)] == [0x599ed017fb08fc85, 0x2c73f08458540fa5, 0x883ebce5a3f27c77]
    assert SplitMix64(-5).next_u64() == 0x16b1cba95fc60262   # negative seeds wrap to u64


def test_helpers_are_defined_from_next_u64():
    r = SplitMix64(42)
    assert [r.random() for _ in range(3)] == [0.7415648787718233, 0.1599103928769201, 0.27860113025513866]
    r = SplitMix64(7)
    assert r.uniform(-1, 1) == -0.22034050321745702
    assert r.randrange(10) == 0
    assert r.randint(2, 5) == 5
    assert r.choice((3, 3, 4)) == 3
    assert r.sample(range(10), 4) == [4, 3, 5, 2]


def test_sample_is_distinct_and_bounded():
    r = SplitMix64(9)
    s = r.sample(range(5), 9)
    assert sorted(s) == [0, 1, 2, 3, 4]
