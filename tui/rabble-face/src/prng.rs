//! SplitMix64, the face's one portable PRNG. Same algorithm and helper definitions as `ui/prng.py`;
//! the known-answer tests below assert the numbers `tests/test_ui_prng.py` asserts.

const GAMMA: u64 = 0x9E37_79B9_7F4A_7C15;

#[derive(Debug, Clone)]
pub struct SplitMix64 {
    state: u64,
}

impl SplitMix64 {
    /// Negative seeds wrap (two's complement), like Python's `seed & MASK`.
    pub fn new(seed: i64) -> Self {
        SplitMix64 { state: seed as u64 }
    }

    pub fn next_u64(&mut self) -> u64 {
        self.state = self.state.wrapping_add(GAMMA);
        let mut z = self.state;
        z = (z ^ (z >> 30)).wrapping_mul(0xBF58_476D_1CE4_E5B9);
        z = (z ^ (z >> 27)).wrapping_mul(0x94D0_49BB_1331_11EB);
        z ^ (z >> 31)
    }

    /// [0, 1): the top 53 bits.
    pub fn random(&mut self) -> f64 {
        (self.next_u64() >> 11) as f64 * (1.0 / 9007199254740992.0)
    }

    pub fn uniform(&mut self, a: f64, b: f64) -> f64 {
        a + (b - a) * self.random()
    }

    /// 0 <= i < n, as floor(random() * n).
    pub fn randrange(&mut self, n: usize) -> usize {
        (self.random() * n as f64) as usize
    }

    /// a <= i <= b.
    pub fn randint(&mut self, a: i64, b: i64) -> i64 {
        a + self.randrange((b - a + 1) as usize) as i64
    }

    pub fn choice<'a, T>(&mut self, seq: &'a [T]) -> &'a T {
        &seq[self.randrange(seq.len())]
    }

    /// k distinct items: a partial Fisher-Yates over a copy.
    pub fn sample<T: Clone>(&mut self, population: &[T], k: usize) -> Vec<T> {
        let mut pool = population.to_vec();
        let n = pool.len();
        for i in 0..k.min(n) {
            let j = i + self.randrange(n - i);
            pool.swap(i, j);
        }
        pool.truncate(k);
        pool
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn splitmix64_reference_values() {
        let mut r = SplitMix64::new(0);
        assert_eq!([r.next_u64(), r.next_u64(), r.next_u64()], [0xe220a8397b1dcdaf, 0x6e789e6aa1b965f4, 0x06c45d188009454f]);
        let mut r = SplitMix64::new(1234567);
        assert_eq!([r.next_u64(), r.next_u64(), r.next_u64()], [0x599ed017fb08fc85, 0x2c73f08458540fa5, 0x883ebce5a3f27c77]);
        assert_eq!(SplitMix64::new(-5).next_u64(), 0x16b1cba95fc60262);
    }

    #[test]
    fn helpers_match_python() {
        let mut r = SplitMix64::new(42);
        assert_eq!([r.random(), r.random(), r.random()], [0.7415648787718233, 0.1599103928769201, 0.27860113025513866]);
        let mut r = SplitMix64::new(7);
        assert_eq!(r.uniform(-1.0, 1.0), -0.22034050321745702);
        assert_eq!(r.randrange(10), 0);
        assert_eq!(r.randint(2, 5), 5);
        assert_eq!(*r.choice(&[3, 3, 4]), 3);
        let pop: Vec<i32> = (0..10).collect();
        assert_eq!(r.sample(&pop, 4), vec![4, 3, 5, 2]);
    }

    #[test]
    fn sample_is_distinct_and_bounded() {
        let mut r = SplitMix64::new(9);
        let mut s = r.sample(&[0, 1, 2, 3, 4], 9);
        s.sort();
        assert_eq!(s, vec![0, 1, 2, 3, 4]);
    }
}
